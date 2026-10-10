# Souvenir: download the tools and data the site needs (into tools/, tiles/, models/).
#
#   make install   pmtiles tool, world map (zoom 0-10, ~3.8 GB) and time-zone grid
#   make france    detailed map of France (zoom 0-15, ~10 GB)
#   make search    llama.cpp (CPU) and the search model (~1.9 GB), started by the server
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

WORLD_MAXZOOM := 10
FRANCE_BBOX   := -5.8,41.2,9.8,51.3
THREADS       := 8

# Latest daily build of the planet at build.protomaps.com (checked only when a map is made).
BUILD ?= $(shell for d in 0 1 2 3 4 5 6; do b=$$(date -d "-$$d day" +%Y%m%d); \
	curl -sfI https://build.protomaps.com/$$b.pmtiles >/dev/null && echo $$b && break; done)
PLANET = https://build.protomaps.com/$(BUILD).pmtiles

.PHONY: install france search run help
.DELETE_ON_ERROR:

# Search queries run on this CPU (llama.cpp, see make search). To use a GPU model
# instead: make run EMBED_API=http://HOST:8000/v1 (and SOUVENIR_API_KEY if it needs a key).
EMBED_API ?=
run:
	python3 -u server.py $(if $(EMBED_API),--embed-api $(EMBED_API))

install: $(PMTILES) tiles/world.pmtiles tiles/timezones.bin
	@echo "Ready. Start the site with: python3 server.py"

france: $(PMTILES) tiles/france.pmtiles

search: tools/llama/llama-server $(SEARCH_MODEL)
	@echo "Ready: the server starts the search model when cache/embeddings.* exist (tools/embed_photos.py)."

service:
	tools/install_service.sh --password --linger

help:
	@sed -n '2,9p' Makefile | sed 's/^# \{0,1\}//'

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
