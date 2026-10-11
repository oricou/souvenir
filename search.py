"""Search photos by meaning: the query sentence and the photos are vectors in the
same space (a multimodal embedding model); the closest photos are the answers.

The photos' vectors are made once by tools/embed_photos.py (cache/embeddings.*).
For each search, the query is turned into a vector by the same model and compared
with all photos by dot product (vectors are unit length, so this is the cosine).
The query side runs either:
- locally on the CPU with llama.cpp (installed by `make search`), started by the
  server as a child process when photo vectors exist (~0.3 s per query, 2 GB RAM);
- or on any OpenAI-compatible API given with --embed-api (e.g. vLLM on a GPU).
Needs NumPy; without it, or without the vectors, search is simply off.
"""

import atexit
import ctypes
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "cache", "embeddings")
LLAMA_SERVER = os.path.join(ROOT, "tools", "llama", "llama-server")
LOCAL_MODEL = os.path.join(ROOT, "models", "Qwen3-VL-Embedding-2B.Q8_0.gguf")
LOCAL_MODEL_FOR = "Qwen/Qwen3-VL-Embedding-2B"  # the model the photo vectors must come from
# Recommended by the model's authors: an English instruction, even for other languages.
QUERY_INSTRUCTION = "Retrieve images that match the user's query."
MIN_SCORE = 0.2        # below this, a photo is not considered a match at all
RELATIVE_SCORE = 0.6   # and not if it scores less than 60% of the best match


class SearchError(Exception):
    """Search unavailable or failed (shown to the user)."""


def _post(url, body, key=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class LocalModel:
    """llama.cpp's server on the CPU, as a child process listening on 127.0.0.1.

    Queries are built exactly as vLLM builds them for this model (its chat
    template WITHOUT the final "assistant" line) and sent as token ids, so that
    llama.cpp adds nothing: the vectors then match those of tools/embed_photos.py
    (measured: 0.997-0.999 similarity, same search results).
    """

    def __init__(self, log):
        with socket.socket() as s:  # a free port, chosen by the system
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threads = str(max(1, os.cpu_count() or 1))
        self.proc = subprocess.Popen(
            [LLAMA_SERVER, "-m", LOCAL_MODEL, "--embedding", "--pooling", "last",
             "-c", "512", "-t", threads, "--host", "127.0.0.1", "--port", str(self.port)],
            env={**os.environ, "LD_LIBRARY_PATH": os.path.dirname(LLAMA_SERVER)},
            stdout=open(log, "wb"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            preexec_fn=_die_with_parent)
        atexit.register(self.stop)

    def ready(self):
        if self.proc.poll() is not None:
            raise SearchError("the local search model stopped (see cache/llama-server.log)")
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                return json.load(r).get("status") == "ok"
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def embed(self, text):
        deadline = time.time() + 60  # the model loads in a few seconds after start
        while not self.ready():
            if time.time() > deadline:
                raise SearchError("the local search model is still starting, try again")
            time.sleep(0.3)
        text = text.replace("<|", "< |")  # typed text must never act as a control token
        prompt = (f"<|im_start|>system\n{QUERY_INSTRUCTION}<|im_end|>\n"
                  f"<|im_start|>user\n{text}<|im_end|>\n")
        ids = _post(self.url + "/tokenize",
                    {"content": prompt, "add_special": False, "parse_special": True})["tokens"]
        return _post(self.url + "/v1/embeddings", {"input": [ids]}, timeout=30)["data"][0]["embedding"]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _die_with_parent():
    """In the child: ask Linux to stop it if the server dies, even killed (-9)."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


class Search:
    """api: an OpenAI-compatible embeddings API, or None for the local llama.cpp."""

    def __init__(self, api=None, key=None, base=BASE, timeout=15):
        self.api, self.key, self.base, self.timeout = api and api.rstrip("/"), key, base, timeout
        self.lock = threading.Lock()
        self.stamp = None  # (mtime, size) of the loaded vectors file
        self.names, self.matrix, self.model = [], None, None
        self.local = None
        self.local_problem = None  # why the local model is not used, if so

    def start(self):
        """Start the local model if it is to be used: no --embed-api, photo vectors
        present and made by the model it serves, llama.cpp and the model installed."""
        if self.api or np is None:
            return
        try:
            self._load()
        except SearchError as e:
            self.local_problem = str(e)
            return
        if self.model != LOCAL_MODEL_FOR:
            self.local_problem = (f"photo vectors made by {self.model}, the local model is "
                                  f"{LOCAL_MODEL_FOR}: use --embed-api")
        elif not (os.access(LLAMA_SERVER, os.X_OK) and os.path.isfile(LOCAL_MODEL)):
            self.local_problem = "local search model not installed: run make search"
        else:
            self.local = LocalModel(os.path.join(ROOT, "cache", "llama-server.log"))

    def status(self):
        """Short description for the server's start message."""
        if np is None:
            return "off (needs NumPy: sudo apt install python3-numpy)"
        try:
            self._load()
        except SearchError as e:
            return f"off ({e})"
        where = (f"queries sent to {self.api}" if self.api else
                 f"queries on this CPU (llama.cpp, port {self.local.port})" if self.local else
                 f"off ({self.local_problem})")
        return f"{len(self.names)} photos encoded with {self.model}; {where}"

    def _load(self):
        """(Re)load the vectors when tools/embed_photos.py has written new ones."""
        try:
            st = os.stat(self.base + ".f16")
        except OSError:
            raise SearchError("no photo vectors yet: run tools/embed_photos.py")
        stamp = (st.st_mtime, st.st_size)
        with self.lock:
            if stamp == self.stamp:
                return
            with open(self.base + ".json", encoding="utf-8") as f:
                meta = json.load(f)
            matrix = np.fromfile(self.base + ".f16", dtype="<f2").astype(np.float32)
            matrix = matrix.reshape(-1, meta["dim"])
            if len(matrix) != len(meta["rows"]):
                raise SearchError("photo vectors being written, try again in a moment")
            self.names = [r["name"] for r in meta["rows"]]
            self.matrix, self.model, self.stamp = matrix, meta["model"], stamp

    def _embed_query(self, text):
        try:
            if self.api:
                vector = _post(self.api + "/embeddings", {
                    "model": self.model, "encoding_format": "float", "messages": [
                        {"role": "system", "content": [{"type": "text", "text": QUERY_INSTRUCTION}]},
                        {"role": "user", "content": [{"type": "text", "text": text}]},
                    ]}, self.key, self.timeout)["data"][0]["embedding"]
            elif self.local:
                vector = self.local.embed(text)
            else:
                raise SearchError(self.local_problem or "no search model")
        except urllib.error.HTTPError as e:
            raise SearchError(f"the search model refused the request ({e.code})")
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            raise SearchError(f"the search model is not reachable ({e})")
        vector = np.asarray(vector, dtype=np.float32)
        return vector / (np.linalg.norm(vector) or 1.0)

    def ranked(self, text, among=None):
        """[(name, score)] of the photos matching the query, best first; with `among`
        (photo names, e.g. those of a person), only those, compared with each other."""
        if np is None:
            raise SearchError("search needs NumPy (sudo apt install python3-numpy)")
        self._load()
        rows = np.arange(len(self.names)) if among is None else np.asarray(
            [i for i, n in enumerate(self.names) if n in among], dtype=int)
        if not len(rows):
            return []
        query = self._embed_query(text)
        scores = self.matrix[rows] @ query
        best = float(scores.max())
        keep = np.flatnonzero(scores >= max(MIN_SCORE, best * RELATIVE_SCORE))
        keep = keep[np.argsort(-scores[keep])]
        return [(self.names[rows[i]], round(float(scores[i]), 3)) for i in keep]
