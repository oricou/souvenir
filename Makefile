# Souvenir: download the tools and data the site needs (all into tools/ and tiles/).
#
#   make install   pmtiles tool, world map (zoom 0-10, ~3.8 GB) and time-zone grid
#   make france    detailed map of France (zoom 0-15, ~10 GB)
#   make help      this list
#
# Each file is only made if missing; delete it to make it again. Maps come from
# the most recent Protomaps build of the last week, or BUILD=YYYYMMDD.

PMTILES_VERSION := 1.31.2
PMTILES_SHA256  := 3ed7dbf4ec2e6dfe5e25b6f70d1ffc932729f93c86db353bf514dd71010a312f
PMTILES_URL     := https://github.com/protomaps/go-pmtiles/releases/download/v$(PMTILES_VERSION)/go-pmtiles_$(PMTILES_VERSION)_Linux_x86_64.tar.gz
PMTILES         := tools/pmtiles

WORLD_MAXZOOM := 10
FRANCE_BBOX   := -5.8,41.2,9.8,51.3
THREADS       := 8

# Latest daily build of the planet at build.protomaps.com (checked only when a map is made).
BUILD ?= $(shell for d in 0 1 2 3 4 5 6; do b=$$(date -d "-$$d day" +%Y%m%d); \
	curl -sfI https://build.protomaps.com/$$b.pmtiles >/dev/null && echo $$b && break; done)
PLANET = https://build.protomaps.com/$(BUILD).pmtiles

.PHONY: install france help
.DELETE_ON_ERROR:

run:
	python3 server.py

install: $(PMTILES) tiles/world.pmtiles tiles/timezones.bin
	@echo "Ready. Start the site with: python3 server.py"

france: $(PMTILES) tiles/france.pmtiles

service:
	tools/install_service.sh --password --linger

help:
	@sed -n '2,8p' Makefile | sed 's/^# \{0,1\}//'

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
