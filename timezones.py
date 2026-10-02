"""Time zone of a position, and its offset from UTC at a given time.

Uses the grid built by tools/build_timezones.py (tiles/timezones.bin): one
array lookup gives the zone name, and zoneinfo gives its offset at that date,
summer time included. Where the grid has no zone (rare slivers along borders)
the offset is the longitude rounded to whole hours. Without the grid file,
offset() returns None and the page shows this computer's time, as before.
"""

import os
import struct
import zlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GRID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiles", "timezones.bin")


class TimeZones:
    def __init__(self, path=GRID):
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != b"TZG1":
            raise ValueError(f"{path}: not a time-zone grid")
        self.width, self.height, self.res, count = struct.unpack_from("<HHfH", data, 4)
        pos, self.names = 14, [None]  # zone 0 = none
        for _ in range(count):
            n = data[pos]
            self.names.append(data[pos + 1:pos + 1 + n].decode())
            pos += 1 + n
        self.grid = memoryview(zlib.decompress(data[pos:])).cast("H")
        self.zones = {}  # name -> ZoneInfo (None if this system does not know it)

    def zone_at(self, lat, lon):
        """IANA name of the zone at a position, e.g. "Pacific/Auckland"; None if unknown."""
        r = min(self.height - 1, max(0, int((lat + 90) / self.res)))
        c = int((lon + 180) / self.res) % self.width
        return self.names[self.grid[r * self.width + c]]

    def offset(self, lat, lon, t_ms):
        """Minutes to add to UTC to get the local time at that place and time."""
        name = self.zone_at(lat, lon)
        if name not in self.zones:
            try:
                self.zones[name] = ZoneInfo(name) if name else None
            except (ZoneInfoNotFoundError, ValueError):
                self.zones[name] = None
        zone = self.zones[name]
        if zone is None:
            return round(lon / 15) * 60
        when = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
        return int(zone.utcoffset(when).total_seconds() // 60)


_instance = None


def get():
    """The shared TimeZones, or None if tiles/timezones.bin has not been built."""
    global _instance
    if _instance is None and os.path.isfile(GRID):
        _instance = TimeZones(GRID)
    return _instance


def offset(lat, lon, t_ms):
    tz = get()
    return tz.offset(lat, lon, t_ms) if tz else None
