"""CMCCDB adapter using installed generated protobufs and established validators."""

import base64
import hashlib
import json
import math
import re
from google.protobuf import descriptor, json_format, message_factory
from cmccdb_schema.proto import reaction_pb2, dataset_pb2
from cmccdb_schema import validations, units


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def tokens(pointer):
    if not pointer.startswith("/") or pointer == "/":
        raise ValueError("Expected a canonical JSON pointer")
    result = []
    for token in pointer[1:].split("/"):
        if not token or re.search(r"~(?![01])", token):
            raise ValueError("Malformed JSON pointer escape or empty token")
        result.append(token.replace("~1", "/").replace("~0", "~"))
    return result


def escaped(token):
    return str(token).replace("~", "~0").replace("/", "~1")


def is_map(field):
    return field.message_type is not None and field.message_type.GetOptions().map_entry


def repeated(field):
    return field.is_repeated if hasattr(field, "is_repeated") else field.label == field.LABEL_REPEATED


def field_at(pointer, allow_container=False):
    parts, desc, pos = tokens(pointer), reaction_pb2.Reaction.DESCRIPTOR, 0
    while pos < len(parts):
        name = parts[pos]
        if name not in desc.fields_by_name:
            raise ValueError(f"Unknown schema field {name!r} in {desc.full_name}")
        field = desc.fields_by_name[name]
        pos += 1
        if is_map(field):
            if pos == len(parts):
                if allow_container:
                    return field
                raise ValueError("Claims must address a particular map entry")
            pos += 1
            field = field.message_type.fields_by_name["value"]
        elif repeated(field):
            if pos == len(parts) and allow_container:
                return field
            if pos == len(parts) or not re.fullmatch(r"0|[1-9][0-9]{0,3}", parts[pos]):
                raise ValueError("Repeated fields require an explicit nonnegative index")
            if int(parts[pos]) > 1000:
                raise ValueError("Repeated index exceeds the extraction limit")
            pos += 1
        if pos == len(parts):
            return field
        if field.message_type is None:
            raise ValueError("Cannot address a child of a scalar field")
        desc = field.message_type
    raise ValueError("Empty field path")


def set_pointer(target, pointer, value):
    parts = tokens(pointer)
    field_at(pointer)
    def assign(node, desc, pos):
        field = desc.fields_by_name[parts[pos]]
        name = field.name
        pos += 1
        container, key = node, name
        if is_map(field):
            container = node.setdefault(name, {})
            key = parts[pos]
            pos += 1
            field = field.message_type.fields_by_name["value"]
        elif repeated(field):
            container = node.setdefault(name, [])
            key = int(parts[pos])
            pos += 1
            while len(container) <= key:
                container.append(None)
        old = container[key] if isinstance(container, list) else container.get(key)
        if pos == len(parts):
            if old is not None:
                raise ValueError("Overlapping claims would overwrite an existing value")
            container[key] = value
        else:
            if old is None:
                container[key] = {}
            assign(container[key], field.message_type, pos)
    assign(target, reaction_pb2.Reaction.DESCRIPTOR, 0)


def scalar(field, value):
    t = field.type
    if t == descriptor.FieldDescriptor.TYPE_ENUM:
        if not isinstance(value, str) or value not in field.enum_type.values_by_name:
            raise ValueError(f"Expected one of {list(field.enum_type.values_by_name)}")
    elif t == descriptor.FieldDescriptor.TYPE_BOOL:
        if type(value) is not bool:
            raise ValueError("Expected a Boolean, not an integer or string")
    elif t in (descriptor.FieldDescriptor.TYPE_FLOAT, descriptor.FieldDescriptor.TYPE_DOUBLE):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Expected a finite numeric value")
    elif t == descriptor.FieldDescriptor.TYPE_STRING:
        if type(value) is not str:
            raise ValueError("Expected text")
    elif t == descriptor.FieldDescriptor.TYPE_BYTES:
        if type(value) is not str:
            raise ValueError("Bytes must be base64 text")
        base64.b64decode(value, validate=True)
    else:
        if type(value) is not int:
            raise ValueError("Expected an integer")
        bits = 32 if t in (5, 7, 13, 15, 17) else 64
        unsigned = t in (6, 7, 13, 4)
        lo, hi = (0, 2**bits - 1) if unsigned else (-2**(bits - 1), 2**(bits - 1) - 1)
        if not lo <= value <= hi:
            raise ValueError("Integer is outside the schema's range")
    return value


def normalize_message(desc, data, crystal=False):
    if type(data) is not dict:
        raise ValueError(f"{desc.name} requires an object")
    result, oneofs = {}, set()
    for name, value in data.items():
        if name not in desc.fields_by_name:
            raise ValueError(f"Unknown field {desc.name}.{name}")
        field = desc.fields_by_name[name]
        if field.containing_oneof is not None:
            group = field.containing_oneof.name
            if group in oneofs:
                raise ValueError(f"Conflicting oneof values in {desc.name}.{group}")
            oneofs.add(group)
        if is_map(field):
            if type(value) is not dict or any(type(k) is not str for k in value):
                raise ValueError("Map value requires an object")
            vf = field.message_type.fields_by_name["value"]
            result[name] = {k: normalize_value(vf, v, crystal=crystal or name == "crystal_parameters") for k, v in value.items()}
        elif repeated(field):
            if type(value) is not list or len(value) > 1001:
                raise ValueError("Repeated value requires a bounded array")
            result[name] = [normalize_value(field, v, crystal=crystal or name == "crystal_parameters") for v in value]
        else:
            # Unit aliases are resolved by CMCCDB, never by a model's arithmetic.
            if name == "units" and type(value) is str and value not in field.enum_type.values_by_name:
                cls, number = units.UnitResolver().resolve_unit(value.replace("µ", "μ"))
                if cls.DESCRIPTOR.full_name != desc.full_name:
                    raise ValueError("Units are incompatible with the target message")
                value = field.enum_type.values_by_number[number].name
            result[name] = normalize_value(field, value, crystal=crystal or name == "crystal_parameters")
    if "units" in result and "value" in result and result["units"] != "UNSPECIFIED":
        cls = message_factory.GetMessageClass(desc)
        msg = json_format.ParseDict(result, cls())
        # Stable explicit units for common quantities, with a schema-derived
        # fallback for other quantities. Keep reported units in each claim.
        unit_field = desc.fields_by_name["units"]
        preferred = ("ANGSTROM" if crystal and desc.name == "Length" and "ANGSTROM" in unit_field.enum_type.values_by_name else
                     {"Time": "SECOND", "Mass": "GRAM", "Frequency": "HERTZ"}.get(desc.name))
        canonical_unit = (unit_field.enum_type.values_by_name[preferred].number if preferred else
                          next(v.number for v in unit_field.enum_type.values if v.number))
        # An identity conversion needs no conversion table. Some supported
        # schema quantities (RAM g-force, for example) have a single defined
        # unit but no entry in the schema's multiplier resolver.
        if msg.units != canonical_unit:
            msg = units.UnitResolver().convert(msg, canonical_unit)
        result = json_format.MessageToDict(msg, preserving_proto_field_name=True)
    return result


def normalize_value(field, value, crystal=False):
    if field.message_type is not None:
        return normalize_message(field.message_type, value, crystal=crystal)
    return scalar(field, value)


def normalize_claim(pointer, value):
    # Run-generated identities/provenance cannot be supplied as purported paper facts.
    forbidden = ["/reaction_id", "/provenance/record_created", "/provenance/record_modified",
                 "/provenance/is_mined", "/provenance/reaction_metadata"]
    if any(pointer == p or pointer.startswith(p + "/") for p in forbidden) or pointer == "/provenance":
        raise ValueError("This field is controlled by the run, not by extraction workers")
    result = normalize_value(field_at(pointer), value, crystal="crystal_parameters" in tokens(pointer))
    canonical(result)  # Also catches NaN and non-JSON payloads.
    return result


def schema_hash():
    return hashlib.sha256(reaction_pb2.DESCRIPTOR.serialized_pb + dataset_pb2.DESCRIPTOR.serialized_pb).hexdigest()


def catalog(prefix="", limit=200, offset=0):
    # Catalog paths use placeholders; concrete claim paths still resolve to the
    # same fields when a task targets a particular input or repeated element.
    if prefix:
        parts, normalized, desc, i = tokens(prefix), [], reaction_pb2.Reaction.DESCRIPTOR, 0
        while i < len(parts):
            field = desc.fields_by_name.get(parts[i])
            if field is None:
                raise ValueError("Unknown catalog prefix")
            normalized.append(field.name)
            i += 1
            if is_map(field):
                if i < len(parts):
                    normalized.append("{key}")
                    i += 1
                desc = field.message_type.fields_by_name["value"].message_type
            else:
                if repeated(field) and i < len(parts):
                    normalized.append("{index}")
                    i += 1
                desc = field.message_type
            if desc is None and i < len(parts):
                raise ValueError("Catalog prefix continues beyond a scalar")
        prefix = "/" + "/".join(normalized)
    rows = []
    def walk(desc, path, seen):
        if desc.full_name in seen:
            return
        for f in desc.fields:
            p = path + "/" + f.name
            row = dict(path=p, type=f.message_type.name if f.message_type else f.type,
                       repeated=repeated(f), oneof=f.containing_oneof.name if f.containing_oneof else None)
            if f.enum_type:
                row["enum"] = list(f.enum_type.values_by_name)
            if not prefix or p.startswith(prefix) or prefix.startswith(p + "/"):
                rows.append(row)
            if f.message_type:
                if is_map(f):
                    vf = f.message_type.fields_by_name["value"]
                    if vf.message_type:
                        walk(vf.message_type, p + "/{key}", seen | {desc.full_name})
                else:
                    walk(f.message_type, p + ("/{index}" if repeated(f) else ""), seen | {desc.full_name})
    walk(reaction_pb2.Reaction.DESCRIPTOR, "", set())
    return dict(schema_sha256=schema_hash(), fields=rows[offset:offset+limit], total=len(rows),
                truncated=len(rows) > offset+limit, next_offset=offset+limit if len(rows) > offset+limit else None,
                conventions={"percentage": "50% is value 50", "extruder_speed": "frequency with RPM",
                             "feed_rate": "volumetric; cm^3/min equals mL/min",
                             "missing": "Never replace missing information with zero",
                             "canonical_units": "Time SECOND; Mass GRAM; Frequency HERTZ; other quantities first nonzero schema unit",
                             "measurements": "Preserve yield, conversion, intensity and recovery distinctions",
                             "crystal": "Source-supported cell lengths use ANGSTROM when available; never invent periodic SMILES/CSD/cell parameters",
                             "chemistry": "SMILES syntax does not establish chemistry approval; current source-grounded manager review is mandatory"})


def dataset_from_json(data):
    return json_format.ParseDict(data, dataset_pb2.Dataset())


def validate_dataset(data):
    proto = dataset_from_json(data)
    output = validations.validate_message(proto, raise_on_error=False,
        options=validations.ValidationOptions(validate_ids=True, require_provenance=True,
                                             allow_reaction_smiles_only=False))
    normalized = json_format.MessageToDict(proto, preserving_proto_field_name=True)
    return normalized, dict(valid=not output.errors, errors=output.errors, warnings=output.warnings)
