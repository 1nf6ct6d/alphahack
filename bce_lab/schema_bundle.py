"""Самодостаточные варианты схемы ответа LLM (без внешних $ref).

full   — исходная схема + определения из common.v1 внутри $defs; локальные $ref. Для API с полной поддержкой JSON Schema
         и для локальной проверки (источник правды).
basic  — урезанная схема: все $ref подставлены, удалены ключевые слова, которые часто не поддерживаются structured output
         (pattern, min/maxLength, min/maxItems, uniqueItems, minimum/maximum, format, $schema, $id, description, title).
         Строгие ограничения при таком режиме проверяются только локально.
Режимы API (выбираются в конфигурации модели): json_schema (full), json_schema_basic (basic), json_object, none.
"""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMON_ID = "https://bce.local/schemas/common.v1.json"
STRIP = {"pattern", "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "minimum", "maximum",
         "format", "$schema", "$id", "description", "title", "default"}


def _load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _rewrite_refs(node):
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if k == "$ref" and isinstance(v, str) and v.startswith(COMMON_ID + "#"):
                node[k] = v[len(COMMON_ID):]
            else:
                _rewrite_refs(v)
    elif isinstance(node, list):
        for v in node:
            _rewrite_refs(v)


def bundle_full():
    out = copy.deepcopy(_load("schemas/llm_hypotheses_output.v1.schema.json"))
    common = _load("schemas/common.v1.schema.json")
    defs = out.setdefault("$defs", {})
    for k, v in common["$defs"].items():
        if k in defs:
            raise ValueError(f"Коллизия имён в $defs: {k}")
        defs[k] = copy.deepcopy(v)
    _rewrite_refs(out)
    out["$id"] = "https://bce.local/schemas/llm_hypotheses_output.v1.bundled.json"
    return out


def _resolve(ref, root):
    node = root
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _inline(node, root, depth=0):
    if depth > 20:
        raise ValueError("Слишком глубокая вложенность $ref (цикл?)")
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(copy.deepcopy(_resolve(node["$ref"], root)), root, depth + 1)
        return {k: _inline(v, root, depth + 1) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline(v, root, depth + 1) for v in node]
    return node


def _strip(node):
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in STRIP or isinstance(v, dict) and k == "properties"}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def bundle_basic():
    full = bundle_full()
    inlined = _inline(full, full)
    # properties с именем из STRIP (напр. "description") здесь не встречаются, но защищаемся
    return _strip(inlined)


def unresolved_refs(schema):
    bad = []

    def walk(n):
        if isinstance(n, dict):
            r = n.get("$ref")
            if r is not None:
                if not (isinstance(r, str) and r.startswith("#")):
                    bad.append(r)
                else:
                    try:
                        _resolve(r, schema)
                    except (KeyError, TypeError):
                        bad.append(r)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(schema)
    return bad


def write_all():
    out = ROOT / "schemas" / "generated"
    out.mkdir(exist_ok=True)
    paths = {}
    for name, fn in (("llm_output.bundled.schema.json", bundle_full), ("llm_output.basic.schema.json", bundle_basic)):
        p = out / name
        p.write_text(json.dumps(fn(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths[name] = p
    return paths


def compact(schema):
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
