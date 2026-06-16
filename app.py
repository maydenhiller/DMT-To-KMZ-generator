"""
DMT To KMZ Generator  (Streamlit app)

Upload a DeLorme Street Atlas .dmt and download a Google Earth .kmz with
AGMs (purple triangles / red flags / blue dots), Access (blue lines),
Centerline (red lines) and Notes (map-note symbols + red X "Do Not Enter").
The Gibson Integrity logo is embedded in every output file.

Icons and colours follow the Google Earth seed-file legend.
Single-file app; only dependency is streamlit.
"""

import base64
import os
import streamlit as st

import struct
import re
import zipfile
import io


# --------------------------------------------------------------------------
# Minimal OLE2 (Compound File Binary) reader
# --------------------------------------------------------------------------
class OLEFile:
    def __init__(self, data):
        if data[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
            raise ValueError("Not an OLE compound file (.dmt)")
        self.d = data
        self.sect_size = 1 << struct.unpack("<H", data[30:32])[0]
        self.mini_size = 1 << struct.unpack("<H", data[32:34])[0]
        self.dir_start = struct.unpack("<I", data[48:52])[0]
        self.mini_cutoff = struct.unpack("<I", data[56:60])[0]
        self.minifat_start = struct.unpack("<I", data[60:64])[0]
        self.difat_start = struct.unpack("<I", data[68:72])[0]

        difat = list(struct.unpack("<109I", data[76:512]))
        sec = self.difat_start
        guard = 0
        while sec not in (0xFFFFFFFE, 0xFFFFFFFF) and guard < 100000:
            off = 512 + sec * self.sect_size
            arr = struct.unpack("<%dI" % (self.sect_size // 4),
                                data[off:off + self.sect_size])
            difat += list(arr[:-1])
            sec = arr[-1]
            guard += 1
        self.difat = [x for x in difat if x not in (0xFFFFFFFF, 0xFFFFFFFE)]

        self.fat = []
        for s in self.difat:
            off = 512 + s * self.sect_size
            self.fat += list(struct.unpack("<%dI" % (self.sect_size // 4),
                                           data[off:off + self.sect_size]))

        self.dir_data = self._read_chain(self.dir_start)
        self._parse_dir()
        mf = self._read_chain(self.minifat_start)
        self.minifat = list(struct.unpack("<%dI" % (len(mf) // 4), mf))
        self.ministream = self._read_chain(self.root_start)

    def _read_chain(self, start):
        out = bytearray()
        s = start
        guard = 0
        while s not in (0xFFFFFFFE, 0xFFFFFFFF) and guard < 10000000:
            off = 512 + s * self.sect_size
            out += self.d[off:off + self.sect_size]
            s = self.fat[s] if s < len(self.fat) else 0xFFFFFFFE
            guard += 1
        return bytes(out)

    def _read_mini(self, start, size):
        out = bytearray()
        s = start
        guard = 0
        while s not in (0xFFFFFFFE, 0xFFFFFFFF) and guard < 10000000:
            off = s * self.mini_size
            out += self.ministream[off:off + self.mini_size]
            s = self.minifat[s] if s < len(self.minifat) else 0xFFFFFFFE
            guard += 1
        return bytes(out[:size])

    def _parse_dir(self):
        self.entries = []
        self.root_start = 0
        for i in range(len(self.dir_data) // 128):
            e = self.dir_data[i * 128:(i + 1) * 128]
            nlen = struct.unpack("<H", e[64:66])[0]
            name = e[:max(0, nlen - 2)].decode("utf-16-le", "replace") if nlen > 0 else ""
            typ = e[66]
            start = struct.unpack("<I", e[116:120])[0]
            size = struct.unpack("<Q", e[120:128])[0]
            self.entries.append((name, typ, start, size))
            if typ == 5:
                self.root_start = start

    def get(self, name):
        for nm, typ, start, size in self.entries:
            if nm == name and typ == 2:
                if size < self.mini_cutoff:
                    return self._read_mini(start, size)
                return self._read_chain(start)[:size]
        return None

    def stream_names(self):
        return [(nm, size) for nm, typ, start, size in self.entries if typ == 2]


# --------------------------------------------------------------------------
# Coordinate decoding (int32, 2**-23 deg grid, +/-256 offset)
# --------------------------------------------------------------------------
_LON_M = 1.192092884267e-07
_LON_B = -255.99999855
_LAT_M = -1.192092922548e-07
_LAT_B = 256.00000471


def _dec_lon(x):
    return x * _LON_M + _LON_B


def _dec_lat(x):
    return x * _LAT_M + _LAT_B


def _valid(lon, lat):
    return -180.0 < lon < 180.0 and -85.0 < lat < 85.0


# --------------------------------------------------------------------------
# Layer parsers
# --------------------------------------------------------------------------
# DeLorme symbol code -> meaning (matches the seed-file legend)
SYMBOL_CODE = {
    0x01: "triangle",   # purple triangle  -> AGM
    0x02: "flag",       # red flag         -> AGM
    0x04: "dot",        # blue dot         -> AGM
    0x03: "redx",       # red X            -> Note "Do Not Enter"
}

_POINT_MARKER = re.compile(rb"\x00\x01\x41")     # <code> 00 01 41
_NOTE_MARKER = re.compile(rb"\x0d\x00\x00\x41")  # text map-note object


def _cstr(buf, p):
    if p < 0 or p >= len(buf):
        return ""
    e = buf.find(b"\x00", p)
    if e < 0:
        e = len(buf)
    try:
        return buf[p:e].decode("latin1").strip()
    except Exception:
        return ""


def parse_points(buf):
    out = []
    for m in _POINT_MARKER.finditer(buf):
        ms = m.start()
        code = buf[ms - 1] if ms >= 1 else 0
        cb = ms - 19
        if cb < 0:
            continue
        try:
            lon = _dec_lon(struct.unpack("<i", buf[cb:cb + 4])[0])
            lat = _dec_lat(struct.unpack("<i", buf[cb + 4:cb + 8])[0])
        except struct.error:
            continue
        if not _valid(lon, lat):
            continue
        out.append({"code": code, "name": _cstr(buf, ms + 21),
                    "lon": lon, "lat": lat})
    return out


def parse_text_notes(buf):
    out = []
    for m in _NOTE_MARKER.finditer(buf):
        ms = m.start()
        cb = ms - 18
        if cb < 0:
            continue
        try:
            lon = _dec_lon(struct.unpack("<i", buf[cb:cb + 4])[0])
            lat = _dec_lat(struct.unpack("<i", buf[cb + 4:cb + 8])[0])
        except struct.error:
            continue
        if not _valid(lon, lat):
            continue
        out.append({"name": _cstr(buf, ms + 22), "lon": lon, "lat": lat})
    return out


_VTX_SEP = bytes.fromhex("0000010000000000")


def parse_lines(buf):
    lines = []
    cur = []
    i = 0
    N = len(buf)
    while i + 8 <= N:
        try:
            lon = _dec_lon(struct.unpack("<i", buf[i:i + 4])[0])
            lat = _dec_lat(struct.unpack("<i", buf[i + 4:i + 8])[0])
        except struct.error:
            break
        if _valid(lon, lat) and i + 16 <= N and buf[i + 8:i + 16] == _VTX_SEP:
            cur.append((lon, lat))
            i += 16
        elif cur and _valid(lon, lat):
            cur.append((lon, lat))
            lines.append(cur)
            cur = []
            i += 8
        else:
            if len(cur) >= 2:
                lines.append(cur)
            cur = []
            i += 1
    if len(cur) >= 2:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------
# Stream -> layer classification
# --------------------------------------------------------------------------
def classify_stream(name, buf):
    n = name.lower()
    if "note" in n:
        return "notes"
    if "agm" in n:
        return "agm"
    if "access" in n:
        return "access"
    if "centerline" in n or "center" in n or re.search(r"\bcl\b|cl\d", n):
        return "centerline"

    n_pts = len(_POINT_MARKER.findall(buf))
    n_notes = len(_NOTE_MARKER.findall(buf))
    n_vtx = buf.count(_VTX_SEP)
    if n_vtx > max(50, n_pts * 5):
        ce = buf.count(b"\x0e\x00\x00\x41")
        ac = max(buf.count(b"\x0f\x00\x00\x41"), buf.count(b"\x11\x00\x00\x41"))
        return "centerline" if ce >= ac else "access"
    if n_notes > 0:
        return "notes"
    if n_pts > 0:
        return "agm"
    return None


# --------------------------------------------------------------------------
# KML / KMZ writer  (icons + colours follow the Google Earth seed legend)
# --------------------------------------------------------------------------
def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# AGM icon styles  (KML colour = AABBGGRR)
#   purple triangle, red flag, blue dot  (per seed "AGM Type" sheet)
_AGM_STYLES = {
    "triangle": ("https://maps.google.com/mapfiles/kml/shapes/triangle.png", "ff800080"),
    "flag":     ("https://maps.google.com/mapfiles/kml/shapes/flag.png",     "ff0000ff"),
    "dot":      ("https://maps.google.com/mapfiles/kml/paddle/blu-circle.png", "ffff0000"),
}

def agm_kind(code):
    """Map a DeLorme AGM symbol code to its Google Earth icon kind.

    Across jobs the codes are not fixed for flags (Red Flag is id 2 in some
    files, id 3 in others), but Purple Triangle is always 1 and Blue Dot is
    always 4.  Survey points are the only Blue Dots; everything that is not a
    triangle or a dot is an AGM rebar -> Red Flag.
    """
    if code == 1:
        return "triangle"
    if code == 4:
        return "dot"
    return "flag"

# Notes legend (seed "NOTES" sheet):
#   Map Note    -> icon 40  (pal3/icon54 map-note symbol)
#   Do Not Enter (red X) -> forbidden.png
_MAPNOTE_ICON = "https://maps.google.com/mapfiles/kml/pal3/icon54.png"
_REDX_ICON = "https://maps.google.com/mapfiles/kml/shapes/forbidden.png"
# Line colours (seed "ACCESS"/"CENTERLINE" sheets):
#   Access = Blue (ffff0000)   Centerline = Red (ff0000ff)
_ACCESS_COLOR = "ffff0000"
_CENTERLINE_COLOR = "ff0000ff"


def _point_placemark(name, lon, lat, style_id):
    nm = _esc(name) if name else ""
    return (
        "\t\t\t<Placemark>\n"
        "\t\t\t\t<name>" + nm + "</name>\n"
        "\t\t\t\t<description>" + nm + "</description>\n"
        "\t\t\t\t<styleUrl>#" + style_id + "</styleUrl>\n"
        "\t\t\t\t<Point>\n"
        "\t\t\t\t\t<coordinates>%.7f,%.7f,0</coordinates>\n" % (lon, lat) +
        "\t\t\t\t</Point>\n"
        "\t\t\t</Placemark>\n"
    )


def _line_placemark(idx, verts, style_id):
    coords = " ".join("%.7f,%.7f,0" % (lo, la) for lo, la in verts)
    return (
        "\t\t\t<Placemark>\n"
        "\t\t\t\t<name>Line " + str(idx) + "</name>\n"
        "\t\t\t\t<styleUrl>#" + style_id + "</styleUrl>\n"
        "\t\t\t\t<LineString>\n"
        "\t\t\t\t\t<tessellate>1</tessellate>\n"
        "\t\t\t\t\t<coordinates>" + coords + "</coordinates>\n"
        "\t\t\t\t</LineString>\n"
        "\t\t\t</Placemark>\n"
    )


def build_kml(doc_name, agms, access, centerline, notes, redx, include_logo=True):
    P = []
    P.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    P.append('<kml xmlns="http://www.opengis.net/kml/2.2" '
             'xmlns:gx="http://www.google.com/kml/ext/2.2" '
             'xmlns:kml="http://www.opengis.net/kml/2.2" '
             'xmlns:atom="http://www.w3.org/2005/Atom">\n')
    P.append("<Document>\n")
    P.append("\t<name>" + _esc(doc_name) + "</name>\n")

    # AGM icon styles
    for sid, (href, color) in _AGM_STYLES.items():
        P.append(
            '\t<Style id="agm_' + sid + '">\n'
            "\t\t<IconStyle>\n"
            "\t\t\t<color>" + color + "</color>\n"
            "\t\t\t<scale>1.1</scale>\n"
            "\t\t\t<Icon><href>" + href + "</href></Icon>\n"
            "\t\t</IconStyle>\n"
            "\t\t<BalloonStyle><text>$[name]</text></BalloonStyle>\n"
            "\t</Style>\n"
        )

    # Map-note symbol (name hidden until mouse-over, per seed HideNameUntilMouseOver)
    for tag, scale in (("note_n", "0"), ("note_h", "1")):
        P.append(
            '\t<Style id="' + tag + '">\n'
            "\t\t<IconStyle><scale>1.1</scale>"
            "<Icon><href>" + _MAPNOTE_ICON + "</href></Icon></IconStyle>\n"
            "\t\t<LabelStyle><scale>" + scale + "</scale></LabelStyle>\n"
            "\t\t<BalloonStyle><text>$[name]</text></BalloonStyle>\n"
            "\t</Style>\n"
        )
    P.append(
        '\t<StyleMap id="note_style">\n'
        "\t\t<Pair><key>normal</key><styleUrl>#note_n</styleUrl></Pair>\n"
        "\t\t<Pair><key>highlight</key><styleUrl>#note_h</styleUrl></Pair>\n"
        "\t</StyleMap>\n"
    )
    # Red X / Do Not Enter
    P.append(
        '\t<Style id="redx_style">\n'
        "\t\t<IconStyle><scale>1.1</scale>"
        "<Icon><href>" + _REDX_ICON + "</href></Icon></IconStyle>\n"
        "\t\t<BalloonStyle><text>$[name]</text></BalloonStyle>\n"
        "\t</Style>\n"
    )
    # Line styles
    P.append('\t<Style id="access_line">\n'
             "\t\t<LineStyle><color>" + _ACCESS_COLOR + "</color>"
             "<width>3</width></LineStyle>\n\t</Style>\n")
    P.append('\t<Style id="centerline_line">\n'
             "\t\t<LineStyle><color>" + _CENTERLINE_COLOR + "</color>"
             "<width>3</width></LineStyle>\n\t</Style>\n")

    if include_logo:
        P.append(
            "\t<ScreenOverlay>\n"
            "\t\t<name>Gibson Logo</name>\n"
            "\t\t<open>1</open>\n"
            "\t\t<Icon><href>files/logo.png</href></Icon>\n"
            '\t\t<overlayXY x="0" y="0" xunits="fraction" yunits="fraction"/>\n'
            '\t\t<screenXY x="25" y="95" xunits="pixels" yunits="pixels"/>\n'
            '\t\t<rotationXY x="0.5" y="0.5" xunits="fraction" yunits="fraction"/>\n'
            '\t\t<size x="300" y="0" xunits="pixels" yunits="pixels"/>\n'
            "\t</ScreenOverlay>\n"
        )

    # AGMs
    P.append("\t<Folder>\n\t\t<name>AGMs</name>\n")
    for p in agms:
        kind = agm_kind(p["code"])
        P.append(_point_placemark(p["name"], p["lon"], p["lat"], "agm_" + kind))
    P.append("\t</Folder>\n")

    # Access (blue)
    P.append("\t<Folder>\n\t\t<name>Access</name>\n")
    for i, verts in enumerate(access, 1):
        P.append(_line_placemark(i, verts, "access_line"))
    P.append("\t</Folder>\n")

    # Centerline (red)
    P.append("\t<Folder>\n\t\t<name>Centerline</name>\n")
    for i, verts in enumerate(centerline, 1):
        P.append(_line_placemark(i, verts, "centerline_line"))
    P.append("\t</Folder>\n")

    # Notes: map-note text + red X
    P.append("\t<Folder>\n\t\t<name>Notes</name>\n")
    for p in notes:
        P.append(_point_placemark(p["name"], p["lon"], p["lat"], "note_style"))
    for p in redx:
        P.append(_point_placemark(p["name"], p["lon"], p["lat"], "redx_style"))
    P.append("\t</Folder>\n")

    P.append("</Document>\n</kml>\n")
    return "".join(P)


def convert_dmt_bytes(dmt_bytes, doc_name="DMT_Export", logo_png=None):
    ole = OLEFile(dmt_bytes)
    agms, access, centerline, notes, redx = [], [], [], [], []

    for name, size in ole.stream_names():
        if size < 16:
            continue
        buf = ole.get(name)
        if not buf:
            continue
        layer = classify_stream(name, buf)
        if layer == "agm":
            agms += parse_points(buf)
        elif layer == "access":
            access += parse_lines(buf)
        elif layer == "centerline":
            centerline += parse_lines(buf)
        elif layer == "notes":
            notes += parse_text_notes(buf)
            # symbol points in the Notes layer are red X "Do Not Enter" markers
            redx += parse_points(buf)

    kml = build_kml(doc_name, agms, access, centerline, notes, redx,
                    include_logo=logo_png is not None)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
        if logo_png:
            z.writestr("files/logo.png", logo_png)
    stats = {
        "AGMs": len(agms),
        "Access lines": len(access),
        "Access vertices": sum(len(l) for l in access),
        "Centerline lines": len(centerline),
        "Centerline vertices": sum(len(l) for l in centerline),
        "Map notes": len(notes),
        "Red X": len(redx),
    }
    return out.getvalue(), stats


# --- Embedded Gibson Integrity logo (PNG, base64) ---
_GIBSON_LOGO_B64 = (
    "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"
)
GIBSON_LOGO_PNG = base64.b64decode(_GIBSON_LOGO_B64)


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="DMT To KMZ Generator", page_icon="🗺")
st.title("DMT To KMZ Generator")
st.caption(
    "Convert a DeLorme Street Atlas .dmt into a Google Earth .kmz. "
    "Purple triangles, red flags and blue dots become AGMs; blue lines "
    "become Access; red lines become Centerline; map notes and red X's "
    "become Notes. The Gibson Integrity logo is embedded in every output."
)

uploaded = st.file_uploader("Upload DMT file", type=["dmt"])

if uploaded is None:
    st.info("Upload a .dmt to begin.")
else:
    base = os.path.splitext(uploaded.name)[0]
    try:
        dmt_bytes = uploaded.read()
        kmz_bytes, stats = convert_dmt_bytes(
            dmt_bytes, doc_name=base, logo_png=GIBSON_LOGO_PNG
        )
    except Exception as e:
        st.error("Could not convert this file: %s" % e)
    else:
        st.success("Conversion complete.")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("AGMs", stats["AGMs"])
        c2.metric("Access lines", stats["Access lines"])
        c3.metric("Centerline lines", stats["Centerline lines"])
        c4.metric("Notes", stats["Map notes"] + stats["Red X"])
        st.caption(
            "Map notes: %d \u2022 Red X: %d \u2022 Access vertices: %s \u2022 "
            "Centerline vertices: %s" % (
                stats["Map notes"], stats["Red X"],
                format(stats["Access vertices"], ","),
                format(stats["Centerline vertices"], ","),
            )
        )
        st.download_button(
            "Download KMZ",
            data=kmz_bytes,
            file_name="%s.kmz" % base,
            mime="application/vnd.google-earth.kmz",
        )
