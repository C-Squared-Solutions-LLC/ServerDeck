"""Careful editors for game settings files: set the values ServerDeck has to own and leave every other line -
comments, order, spacing, encoding, line endings - as the game or the admin wrote it.

  ini      [Section] Key=Value          key "Section/Key" (or "Key": its first line anywhere, else the end)
  cfg      Source-style  name "value"   (server.cfg; "//" comments)
  xmlprop  <property name="Key" value="..."/>          (7 Days to Die serverconfig.xml)
  xml      element text "a/b/c" or attribute "a/b@attr" (ElementTree; comments kept)
  json     dotted keys "a.b.c" ("servers.0.ip" for a list item); values keep their type
  yaml     "Key: value" lines (keys unique in the file, like Empyrion's dedicated.yaml), "Section/Key" to
           add a missing one under its section

edit(text, fmt, values) -> new text. Each editor adds keys it can't find.
"""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def read(path):
    """(text, encoding) - UTF-16 if the file has that BOM, else UTF-8 (a BOM is kept)."""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    return raw.decode("utf-8", errors="replace"), "utf-8"


def write(path, text, encoding="utf-8"):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(text.encode(encoding))


def _nl(text):
    return "\r\n" if "\r\n" in text else "\n"


def _s(value):
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


# ---- ini -------------------------------------------------------------------

def ini(text, values, bools=("True", "False")):
    nl = _nl(text)
    lines = text.split(nl) if text else []
    for key, value in values.items():
        section, _, name = key.rpartition("/")
        value = (bools[0] if value else bools[1]) if isinstance(value, bool) else str(value)
        cur, start, end, done = None, None, None, False
        for i, line in enumerate(lines):
            m = re.match(r"^\s*\[(.+?)\]\s*$", line)
            if m:
                if start is not None and end is None:
                    end = i
                cur = m.group(1)
                if section and cur.lower() == section.lower() and start is None:
                    start = i
                continue
            if section and (cur or "").lower() != section.lower():
                continue
            m = re.match(r"^(\s*)(%s)(\s*)=" % re.escape(name), line, re.I)
            if m:
                lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}={value}"
                done = True
                break
        if done:
            continue
        if section and start is None:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ([""] if lines else []) + [f"[{section}]", f"{name}={value}"]
        else:
            at = end if end is not None else len(lines)
            while at > (start + 1 if start is not None else 0) and not lines[at - 1].strip():
                at -= 1
            lines.insert(at, f"{name}={value}")
    out = nl.join(lines)
    return out if not lines or out.endswith(nl) else out + nl


# ---- Source server.cfg -------------------------------------------------------

def cfg(text, values):
    nl = _nl(text)
    lines = text.split(nl) if text else []
    for name, value in values.items():
        new = f'{name} "{_s(value).replace(chr(34), chr(39))}"'
        for i, line in enumerate(lines):
            if re.match(r"^\s*%s(\s|$)" % re.escape(name), line, re.I):
                lines[i] = new
                break
        else:
            while lines and not lines[-1].strip():
                lines.pop()
            lines.append(new)
    out = nl.join(lines)
    return out if out.endswith(nl) else out + nl


# ---- 7 Days to Die serverconfig.xml: <property name="X" value="Y"/> ----------------

def _xml_attr(value):
    return _s(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def xmlprop(text, values):
    nl = _nl(text)
    for name, value in values.items():
        pat = re.compile(r'(<property\s+name\s*=\s*"%s"\s+value\s*=\s*")[^"]*(")' % re.escape(name))
        comments = [m.span() for m in re.finditer(r"<!--.*?-->", text, re.S)]
        live = next((m for m in pat.finditer(text) if not any(a <= m.start() < b for a, b in comments)), None)
        if live:                                   # (a commented-out example of the same property doesn't count)
            text = text[:live.start()] + live.group(1) + _xml_attr(value) + live.group(2) + text[live.end():]
            continue
        line = f'\t<property name="{name}" value="{_xml_attr(value)}"/>'
        m = re.search(r"^[ \t]*</ServerSettings>", text, re.M)
        if m:
            text = text[:m.start()] + line + nl + text[m.start():]
        else:
            text = text.rstrip() + nl + line + nl
    return text


# ---- xml by path ------------------------------------------------------------

def xml(text, values):
    head = re.match(r"\s*<\?xml[^>]*\?>\s*", text)
    root = ET.fromstring(text, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    for path, value in values.items():
        path, _, attr = path.partition("@")
        node = root
        for part in [p for p in path.split("/") if p]:
            child = node.find(part)
            if child is None:
                child = ET.SubElement(node, part)
            node = child
        if attr:
            node.set(attr, _s(value))
        else:
            node.text = _s(value)
    body = ET.tostring(root, encoding="unicode")
    nl = _nl(text)
    return (head.group(0) if head else "") + body.replace("\n", nl) + nl


# ---- json --------------------------------------------------------------------

def json_(text, values):
    data = json.loads(text) if text.strip() else {}
    for key, value in values.items():
        node, parts = data, key.split(".")
        for p in parts[:-1]:
            node = node[int(p)] if isinstance(node, list) else node.setdefault(p, {})   # "servers.0.ip"
        if isinstance(node, list):
            node[int(parts[-1])] = value
        else:
            node[parts[-1]] = value
    nl = _nl(text) if text else "\n"
    return json.dumps(data, indent=2, ensure_ascii=False).replace("\n", nl) + nl


# ---- yaml (one key per line) --------------------------------------------------------

def _yaml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    if s == "" or re.search(r"[:#'\"{}\[\],&*!|>%@`]|^\s|\s$", s):
        return "'" + s.replace("'", "''") + "'"
    return s


def yaml(text, values):
    nl = _nl(text)
    lines = text.split(nl) if text else []
    for key, value in values.items():
        section, _, name = key.rpartition("/")
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)(%s):(\s*)([^#]*?)(\s*#.*)?$" % re.escape(name), line)
            if m:
                lines[i] = f"{m.group(1)}{m.group(2)}: {_yaml_value(value)}{m.group(5) or ''}"
                break
        else:
            at = None
            if section:
                for i, line in enumerate(lines):
                    if re.match(r"^%s:\s*(#.*)?$" % re.escape(section), line):
                        at = i + 1
                        indent = next((re.match(r"^(\s+)", ln).group(1) for ln in lines[at:at + 5]
                                       if re.match(r"^\s+\S", ln)), "  ")
                        lines.insert(at, f"{indent}{name}: {_yaml_value(value)}")
                        break
                else:
                    lines += [f"{section}:", f"  {name}: {_yaml_value(value)}"]
            else:
                lines.append(f"{name}: {_yaml_value(value)}")
    out = nl.join(lines)
    return out if out.endswith(nl) else out + nl


EDITORS = {"ini": ini, "cfg": cfg, "xmlprop": xmlprop, "xml": xml, "json": json_, "yaml": yaml}


def edit(text, fmt, values):
    if fmt not in EDITORS:
        raise ValueError(f"unknown settings format '{fmt}'")
    return EDITORS[fmt](text, values)
