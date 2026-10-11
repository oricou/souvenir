# Souvenir: download the tools and data the site needs (into tools/, tiles/, models/).
#
#   make install   pmtiles tool, world map (zoom 0-10, ~3.8 GB) and time-zone grid
#   make france    detailed map of France (zoom 0-15, ~10 GB)
#   make search    llama.cpp (CPU) and the search model (~1.9 GB), started by the server
#   make faces     OpenCV (in .venv/) and the face models (~40 MB) for tools/find_faces.py
#   make service   install the souvenir service, or start it, or restart it
#   make clean-cache  remove the previews and face images of photos that are gone
#   make help      this list
#
# Each file is only made if missing; delete it to make it again. Maps come from
# the most recent Protomaps build of the last week, or BUILD=YYYYMMDD.

PMTILES_VERSION := 1.31.2
PMTILES_SHA256  := 3ed7dbf4ec2e6dfe5e25b6f70d1ffc932729f93c86db353bf514dd71010a312f
PMTILES_URL     := https://github.com/protomaps/go-pmtiles/releases/download/v$(PMTILES_VERSION)/go-pmtiles_$(PMTILES_VERSION)_Linux_x86_64.tar.gz
PMTILES         := tools/pmtiles

# Search: llama.cpp's CPU build serves the query side of the embedding model the
# photos were encoded with (tools/embed_photos.py, Qwen3-VL-Embedding-2B).
LLAMA_BUILD   := b11535
LLAMA_SHA256  := 47d25c3a6a9ddfa364f75c02e04f9b2f1ce7676f79bb3c073860bad9168e5057
LLAMA_URL     := https://github.com/ggml-org/llama.cpp/releases/download/$(LLAMA_BUILD)/llama-$(LLAMA_BUILD)-bin-ubuntu-x64.tar.gz
SEARCH_MODEL  := models/Qwen3-VL-Embedding-2B.Q8_0.gguf
SEARCH_SHA256 := 26fadde153b2266d244de4752c2dcba35be78872bdeeb005390b74689d45d851
SEARCH_URL    := https://huggingface.co/mradermacher/Qwen3-VL-Embedding-2B-GGUF/resolve/main/Qwen3-VL-Embedding-2B.Q8_0.gguf

# Faces: OpenCV's YuNet (detection) and SFace (recognition), Apache 2.0. OpenCV comes
# from PyPI into .venv/ (the system's is too old for YuNet 2023mar); the server does
# not need it, only tools/find_faces.py.
OPENCV_VERSION := 4.14.0.94
YUNET          := models/face_detection_yunet_2023mar.onnx
YUNET_SHA256   := 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
YUNET_URL      := https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx
SFACE          := models/face_recognition_sface_2021dec.onnx
SFACE_SHA256   := 0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
SFACE_URL      := https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx

WORLD_MAXZOOM := 10
FRANCE_BBOX   := -5.8,41.2,9.8,51.3
THREADS       := 8

# Latest daily build of the planet at build.protomaps.com (checked only when a map is made).
BUILD ?= $(shell for d in 0 1 2 3 4 5 6; do b=$$(date -d "-$$d day" +%Y%m%d); \
	curl -sfI https://build.protomaps.com/$$b.pmtiles >/dev/null && echo $$b && break; done)
PLANET = https://build.protomaps.com/$(BUILD).pmtiles

.PHONY: install france search faces run service clean-cache help
.DELETE_ON_ERROR:

# Search queries run on this CPU (llama.cpp, see make search). To use a GPU model
# instead: make run EMBED_API=http://HOST:8000/v1 (and SOUVENIR_API_KEY if it needs a key).
EMBED_API ?=
run:
	python3 -u server.py $(if $(EMBED_API),--embed-api $(EMBED_API))

install: $(PMTILES) tiles/world.pmtiles tiles/timezones.bin
	@echo "Ready. Start the site with: python3 server.py"

france: $(PMTILES) tiles/france.pmtiles

faces: .venv/bin/opencv $(YUNET) $(SFACE)
	@echo "Run tools/find_faces.py"
	tools/find_faces.py

search: tools/llama/llama-server $(SEARCH_MODEL)
	@echo "Ready: the server starts the search model when cache/embeddings.* exist (tools/embed_photos.py)."

# The systemd user service: installed if it never was (asks the password for other
# devices), started if it is stopped, restarted if it runs (e.g. after changing server.py);
# then checked: systemd does not wait to see whether the server survives its start.
# Refuses at once if its port is held by another program (e.g. a forgotten make run).
SERVICE ?= souvenir
service:
	@port=$$(systemctl --user cat $(SERVICE) 2>/dev/null | sed -n 's/^ExecStart=.*--port[= ]\([0-9]*\).*/\1/p'); \
	port=$${port:-8080}; \
	if ss -ltnH "sport = :$$port" | grep -q .; then \
		holder=$$(ss -ltnpH "sport = :$$port" | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2); \
		main=$$(systemctl --user show -p MainPID --value $(SERVICE) 2>/dev/null); \
		if [ -z "$$holder" ]; then \
			echo "Port $$port is used by a program of another user: stop it (sudo ss -ltnp | grep :$$port), then run make service again."; \
			exit 1; \
		elif [ "$$holder" != "$$main" ]; then \
			echo "Port $$port is used by another program than $(SERVICE): PID $$holder, $$(ps -o args= -p $$holder)"; \
			echo "Stop it first (Ctrl+C in its terminal, or: kill $$holder), then run make service again."; \
			exit 1; \
		fi; \
	fi; \
	if ! systemctl --user cat $(SERVICE) >/dev/null 2>&1; then \
		echo "Installing user service souvenir"; \
		tools/install_service.sh --password --linger; \
		exit $$?; \
	elif systemctl --user is-active --quiet $(SERVICE); then \
		echo "Restart service souvenir"; \
		systemctl --user restart $(SERVICE) || exit 1; \
	else \
		echo "Start service souvenir"; \
		systemctl --user reset-failed $(SERVICE) 2>/dev/null; \
		systemctl --user start $(SERVICE) || exit 1; \
	fi; \
	sleep 3; \
	if systemctl --user is-active --quiet $(SERVICE); then \
		echo "$(SERVICE) is running"; \
		systemctl --user status $(SERVICE) --no-pager --lines=8; \
	else \
		echo "$(SERVICE) FAILED to start, its last messages:"; \
		journalctl --user -u $(SERVICE) -n 15 --no-pager -o cat; \
		exit 1; \
	fi
clean-cache:
	python3 tools/clean_cache.py

help:
	@sed -n '2,12p' Makefile | sed 's/^# \{0,1\}//'

$(PMTILES):
	@mkdir -p tools
	curl -sL $(PMTILES_URL) -o $@.tar.gz.tmp
	echo "$(PMTILES_SHA256)  $@.tar.gz.tmp" | sha256sum -c - || { rm -f $@.tar.gz.tmp; exit 1; }
	tar xzf $@.tar.gz.tmp -O pmtiles > $@.tmp
	rm -f $@.tar.gz.tmp
	chmod +x $@.tmp && mv $@.tmp $@
	@$@ version

tiles/world.pmtiles: | $(PMTILES)
	@test -n "$(BUILD)" || { echo "no Protomaps build found in the last week (set BUILD=YYYYMMDD)"; exit 1; }
	@mkdir -p tiles
	$(PMTILES) extract $(PLANET) $@.tmp --maxzoom=$(WORLD_MAXZOOM) --download-threads=$(THREADS)
	mv $@.tmp $@

tiles/france.pmtiles: | $(PMTILES)
	@test -n "$(BUILD)" || { echo "no Protomaps build found in the last week (set BUILD=YYYYMMDD)"; exit 1; }
	@mkdir -p tiles
	$(PMTILES) extract $(PLANET) $@.tmp --bbox=$(FRANCE_BBOX) --download-threads=$(THREADS)
	mv $@.tmp $@

tiles/timezones.bin: tools/build_timezones.py
	python3 tools/build_timezones.py --out $@

tools/llama/llama-server:
	@mkdir -p tools/llama
	curl -sL $(LLAMA_URL) -o tools/llama.tar.gz.tmp
	echo "$(LLAMA_SHA256)  tools/llama.tar.gz.tmp" | sha256sum -c - || { rm -f tools/llama.tar.gz.tmp; exit 1; }
	tar xzf tools/llama.tar.gz.tmp -C tools/llama --strip-components=1
	rm -f tools/llama.tar.gz.tmp
	@test -x $@ && echo "llama.cpp $(LLAMA_BUILD) installed in tools/llama/"

$(SEARCH_MODEL):
	@mkdir -p models
	curl -sL $(SEARCH_URL) -o $@.tmp
	echo "$(SEARCH_SHA256)  $@.tmp" | sha256sum -c - || { rm -f $@.tmp; exit 1; }
	mv $@.tmp $@

# A virtual environment for the tools, reusing the system's NumPy and Pillow.
.venv/bin/opencv:
	python3 -m venv --system-site-packages .venv
	.venv/bin/pip install --quiet --disable-pip-version-check opencv-python-headless==$(OPENCV_VERSION)
	.venv/bin/python -c "import cv2; print('OpenCV', cv2.__version__, 'in .venv/')"
	@touch $@

$(YUNET):
	@mkdir -p models
	curl -sL $(YUNET_URL) -o $@.tmp
	echo "$(YUNET_SHA256)  $@.tmp" | sha256sum -c - || { rm -f $@.tmp; exit 1; }
	mv $@.tmp $@

$(SFACE):
	@mkdir -p models
	curl -sL $(SFACE_URL) -o $@.tmp
	echo "$(SFACE_SHA256)  $@.tmp" | sha256sum -c - || { rm -f $@.tmp; exit 1; }
	mv $@.tmp $@
