---
name: freecad-conventions
description: Modelling conventions and FreeCAD property names. Use before creating or editing any FreeCAD geometry through the FreeCAD tools.
---
# FreeCAD conventions

- Units are millimetres. Angles are degrees.
- One PartDesign Body per physical part; name bodies `Body_<PartName>`.
- Fully constrain every sketch before padding or pocketing.

## Primitive property names (create_object / edit_object)

These objects have no `Size` or `Dimensions` property. Use exactly these names:

| obj_type | properties |
|---|---|
| `Part::Box` | `Length`, `Width`, `Height` |
| `Part::Cylinder` | `Radius`, `Height`, `Angle` |
| `Part::Sphere` | `Radius` |
| `Part::Cone` | `Radius1`, `Radius2`, `Height` |
| `Part::Torus` | `Radius1`, `Radius2` |

Position goes in `Placement`: `{"Base": {"x": 0, "y": 0, "z": 0}}`.

Example, a 20 mm cube:
`create_object(doc_name="Doc", obj_type="Part::Box", obj_name="Cube", obj_properties={"Length": 20, "Width": 20, "Height": 20})`

## If a call fails

Read the error, check the object was not half-created (`get_objects`), and delete
or edit the existing object rather than creating a duplicate.
