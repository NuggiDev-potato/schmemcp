# EasyEDA Standard MCP Agent Knowledge Base

This document provides complete reference material for an AI agent operating the EasyEDA Standard schematic/PCB editor through the MCP bridge.

---

## System Architecture

```
[ AI Agent (LLM) ]
        |
        | MCP Tools (stdio)
        v
[ Python MCP Server (bridge_server.py) ]
        |
        |--- Direct HTTP ──> [ LCSC / jlcsearch API ]
        |
        | WebSocket JSON (ws://localhost:8765)
        v
[ EasyEDA Extension (main.js) ]
        |
        | window.api() calls
        v
[ EasyEDA Standard Editor ]
```

---

## Coordinate System

EasyEDA Standard uses internal pixel units:

| Unit | Pixels | Millimeters | Inches |
|------|--------|-------------|--------|
| 1 px | 1 | 0.254 | 0.01 |
| 1 mil | 0.1 | 0.0254 | 0.001 |
| 1 mm | 3.937 | 1 | 0.03937 |

- **X-axis:** Left to right (increasing)
- **Y-axis:** Top to bottom (increasing)

### Unit Conversion via API

```javascript
// Millimeters to pixels
api("unitConvert", { type: "mm2pixel", value: 10 });  // ~39.37

// Mils to pixels
api("unitConvert", { type: "mil2pixel", value: 100 }); // 10
```

---

## Available MCP Tools

### 1. `get_canvas_source(type="json")`

Retrieves the full active EasyEDA document schema.

**Parameters:**
- `type`: `"json"` | `"compress"` | `"svg"` (default: `"json"`)

**Returns:** Complete document JSON with shape objects keyed by `gId`.

**Example response structure:**
```json
{
  "head": { "docType": "1", "editorVersion": "6.5.22", "title": "Main_Board" },
  "canvas": "1000,1000,#FFFFFF,...",
  "TRACK": {
    "gge12": {
      "gId": "gge12",
      "strokeWidth": 1,
      "pointArr": [{"x": 100, "y": 100}, {"x": 200, "y": 100}],
      "net": "VCC"
    }
  }
}
```

### 2. Component Search Tools (Python-native)

All component search runs **entirely in Python** (`requests` → EasyEDA/LCSC HTTP APIs); NO
browser/WebSocket connection is required. Whatever you can describe in free-form features,
the tools translate into a ranked shortlist.

Available tools:

| Tool | Use for |
|------|---------|
| `search_lcsc_component(query)` | Generic keyword/C-number search |
| `search_component(features)` | Any component described by free-form features |
| `search_capacitor(features)` | Capacitors (auto-excludes Y5V/Z5U) |
| `search_resistor(features)` | Resistors |
| `search_diode(features)` | Diodes (incl. schottky, zener) |
| `search_inductor(features)` | Inductors (power/SMD) |

**Parameters (`features`):** a **list of feature strings** describing specs you care about,
e.g. `["boost converter", "600 kHz"]`, `["I2C", "level shifter"]`, `["100nF", "0603", "X7R"]`,
`["10k", "0402", "1%"]`, `["SS34", "schottky", "3A", "40V"]`. Every feature string must show up
in the returned part's title/description/tags/value — parts that don't match ALL features are
discarded (this removes "stray" unrelated results).

**Result object:**
```json
{
  "query": "boost converter 600kHz",
  "attempted": true,
  "max_price": 5.0,
  "candidates": [{
    "lcsc_id": "C123302",
    "title": "TPS61021A ...",
    "value": "3.3V",
    "package": "SOT-23-5",
    "manufacturer": "TI",
    "mfr_part": "TPS61021ADSGR",
    "stock": 12345,
    "price": 0.412,
    "over_budget": false,
    "jlc_class": "Extended Part"
  }]
}
```

The tool already:
- **Remove unavailable parts** (stock = 0 filtered out).
- **Drop stray/unrelated results** that do not actually match the search type.
- **Sort by price ascending** and return the **top 5** (`max_results` still honored).

**Budget rule:** `over_budget: true` means the candidate is above the $5 budget. When the
best match exceeds $5, ASK THE USER FOR THEIR OPINION before placing it.

**Stop rule:** if a search returns no candidates and a retry with a different/better query
also fails, STOP — do not keep guessing queries. Refine by asking the user for more specs
or pick a different component type.

### 3. `place_component(title, x, y)`

Places a schematic library component on the canvas.

**Parameters:**
- `title`: Component name (e.g., `"HDR2X2"`, `"NE555"`)
- `x`: X-coordinate in internal pixels
- `y`: Y-coordinate in internal pixels

**Internal API call:**
```javascript
api("createShape", {
  shapeType: "schlib",
  title: title,
  x: x,
  y: y
});
```

### 4. `add_wire(x1, y1, x2, y2)`

Draws a schematic wire between two points.

**Parameters:**
- `x1, y1`: Start point coordinates (pixels)
- `x2, y2`: End point coordinates (pixels)

**Mechanism (verified working, per official EasyEDA API docs):** Wires are created via
`api("createShape", {shapeType: "wire", jsonCache: {...}})` **containing `pointArr`** (geometry array).
This registers a real logical wire in the top-level `wire` container of the document and renders it
on the canvas with `c_etype="wire"` inside a `<g class="shapeBox">` group — exactly like wires drawn
interactively. Such wires ARE electrically connected to any pins whose endpoints they touch.

CRITICAL differences from earlier (broken) attempts:

- Passing `points` (instead of `pointArr`) inside `jsonCache` — or calling `createShape` without
  `jsonCache` — routes to the interactive `drawShape` state machine and leaves a `false` stub in the
  top-level `wire` container (nothing rendered). Only `jsonCache.pointArr` works.
- Injecting into `src.schlib[<sheetLibGid>].polyline[...]` and calling `applySource` produces a
  COSMETIC polyline (no `c_etype="wire"`): visually renders but is NOT logically connected. It also
  risks dropping `frame_lib_1` from the document during an `applySource` round-trip.

**Internal implementation (main.js `handleAddWire`):**
```javascript
var gid = nextGid(src); // max existing gge N + 1
var ret = api('createShape', {
  shapeType: 'wire',
  jsonCache: {
    gId: gid,
    strokeColor: '#880000',
    strokeWidth: '1',
    strokeStyle: 0,
    fillColor: 'none',
    locked: '0',
    pointArr: [{ x: x1, y: y1 }, { x: x2, y: y2 }]
  }
});
```

Verified in the live editor: the resulting `<polyline c_etype="wire" c_shapetype="line" points="...">`
is a proper net wire; endpoints snapped to pin endpoints are electrically connected.

### 5. `update_net_name(gid, net_name)`

Updates the net assignment of a pad or track element.

**Parameters:**
- `gid`: Global ID of the shape (e.g., `"gge5"`)
- `net_name`: Net name to assign (e.g., `"VCC"`, `"GND"`)

**Internal API call:**
```javascript
api("updateShape", {
  shapeType: "PAD",
  jsonCache: { gId: gid, net: net_name }
});
```

---

## EasyEDA Native API Reference

All operations below are executed via `window.api(commandName, argsObject)` through the browser extension bridge.

### Document State

| API | Description |
|-----|-------------|
| `api("getSource", {type:"json"})` | Get full document as JSON |
| `api("getSource", {type:"compress"})` | Get compressed canvas string |
| `api("getSource", {type:"svg"})` | Get SVG representation |
| `api("applySource", {source:obj, createNew:false})` | Overwrite/modify active canvas |

### Shape Manipulation

| API | Description |
|-----|-------------|
| `api("getShape", {id:"gge13"})` | Get single shape by gId |
| `api("createShape", {...})` | Create and place a shape |
| `api("updateShape", {...})` | Modify shape properties |
| `api("delete", {ids:["gge2","gge3"]})` | Delete shapes by ID |
| `api("clone", {ids:["gge2","gge3"]})` | Clone shapes, returns new IDs |

### Selection & Transform

| API | Description |
|-----|-------------|
| `api("select", {ids:["gge1"]})` | Select objects |
| `api("selectNone")` | Deselect all |
| `api("getSelectedIds")` | Get selected gId array |
| `api("rotate", {ids:["gge1"], degree:90})` | Rotate clockwise |
| `api("fliph", {ids:["gge1"]})` | Flip horizontal |
| `api("flipv", {ids:["gge1"]})` | Flip vertical |
| `api("align_left", {ids:["gge1","gge2"]})` | Align left edges |

### Supported shapeType Values

**Schematic:**
`schlib`, `wire`, `bus`, `netlabel`, `pin`, `junction`, `noconnectflag`, `annotation`, `rect`, `circle`, `polyline`, `path`

**PCB:**
`FOOTPRINT`, `TRACK`, `COPPERAREA`, `SOLIDREGION`, `RECT`, `CIRCLE`, `TEXT`, `VIA`, `PAD`, `HOLE`

### PCB-Specific createShape Examples

**Track:**
```javascript
api("createShape", {
  shapeType: "TRACK",
  layerid: "1",      // 1=Top, 2=Bottom
  net: "GND",
  strokeWidth: 10,   // 10px = 100mil
  pointArr: [{x:100,y:100}, {x:200,y:100}]
});
```

**Via:**
```javascript
api("createShape", {
  shapeType: "VIA",
  net: "GND",
  x: 500, y: 500,
  holeR: 15,         // hole radius in px
  padR: 25           // pad radius in px
});
```

**PAD:**
```javascript
api("createShape", {
  shapeType: "PAD",
  net: "VCC",
  x: 100, y: 100,
  shape: "ELLIPSE",  // ELLIPSE, RECT, OVAL
  holeR: 10,
  padR: 20
});
```

---

## Common Agent Workflows

### 1. Read Current Design

```
1. Call get_canvas_source(type="json")
2. Parse the JSON to identify:
   - Components in "components" or "schlib" sections
   - Wires in "WIRE" section
   - Nets in track/pad "net" fields
   - gIds for each shape
```

### 2. Place a Component

```
1. Search for component: search_lcsc_component("NE555")
2. Choose position (x, y) in internal pixels
3. Call place_component("NE555", 400, 300)
4. Note the returned gId for subsequent operations
```

### 3. Connect Components with Wires

```
1. Get canvas source to find pin locations
2. Call add_wire(x1, y1, x2, y2) between pin endpoints
3. Each wire segment is one call; chain calls for paths
```

### 4. Rename Nets

```
1. Get canvas source to find pad/track gIds
2. Call update_net_name("gge5", "VCC")
3. Verify with get_canvas_source if needed
```

### 5. Modify PCB Track Widths

```
1. Get canvas source (type="json")
2. Iterate json.TRACK objects
3. For each track, modify strokeWidth
4. Apply with applySource({source: modifiedJson, createNew: false})
```

---

## Error Handling

| Error | Cause | Action |
|-------|-------|--------|
| `EASYEDA_API_NOT_READY` | `window.api` undefined | Wait 2s, retry |
| `EXECUTION_FAILED` | Invalid API call | Check shapeType, parameters |
| `ID_NOT_FOUND` | gId doesn't exist | Call getSource to refresh IDs |
| WebSocket timeout | Browser not connected | Ensure extension is loaded, check port 8765 |

---

## WebSocket Protocol

Messages between Python server and browser extension use JSON-RPC style frames:

**Request (Server → Browser):**
```json
{
  "req_id": "abc123",
  "action": "EXECUTE_API",
  "api_name": "getSource",
  "args": {"type": "json"}
}
```

**Response (Browser → Server):**
```json
{
  "req_id": "abc123",
  "status": "success",
  "data": { ... }
}
```

**Error Response:**
```json
{
  "req_id": "abc123",
  "status": "error",
  "error": "EXECUTION_FAILED",
  "message": "Error details"
}
```

---

## Component Selection Guidance

Rules an agent MUST follow when choosing parts:

1. **More than one candidate with equal/close price+stock → ASK the user** which one they
   prefer before finalizing. Do not silently pick between equal ties.
2. **Over-budget pick (> $5)** → surface the price to the user and ask if it is acceptable.
3. **Cannot find a match** → after **2 failed search calls** (with different/good queries),
   STOP. Ask the user for clarification rather than guessing further.
4. Never place a part that is out of stock (the tools already filter these, but double-check
   the `stock` field).
5. Prefer parts with higher stock when candidates are otherwise equivalent — fewer supply risks.

### Dielectrics (SMD ceramic capacitors)

- **Y5V and Z5U — NOT recommended.** They lose most of their capacitance under DC bias and with
  temperature (up to −80%/+30% from −30°C to +85°C, and up to −50% at rated DC voltage), which
  kills decoupling/filtering behavior. `search_capacitor()` automatically excludes them.
- **Preferred dielectrics:** C0G/NP0 (stable, low loss, for precision/timing), X7R, X5R, X6S
  (good density + stability trade-off for decoupling).
- If in doubt for power/decoupling: X7R/X5R.

### Footprint pros/cons

| Package | Good | Bad |
|---------|------|-----|
| 0402 | tiny, high density, cheap | hard to hand-solder, low power/voltage rating |
| 0603 | good density + easy to handle | none major — default choice for passives |
| 0805 | easier to hand-solder, more power | larger, fewer fit on a board |
| 1206 | high power/voltage, easiest hand-solder | large footprint |
| SOT-23-3/5/6 | tiny, common for small ICs/transistors | harder to hand-solder, low power |
| SOIC-8 (150mil) | easy to solder, standard | larger than QFN/SOIC-8EP for power |
| SOIC-8-EP / SOP-8-PP | better thermal payout for power ICs | exposed pad needs correct pad in layout |
| QFN-xx | small, good thermal/electrical | hard to hand-solder, pad under package |
| SOT-223 | higher power than SOT-23, easy solder | bulkier |
| DPAK/TO-252 | good thermal path for regulators/diodes | big for small signals |
| Through-hole (DIP) | breadboard/prototype friendly, easy solder | large, not for production density |
| 1210 / 2010 / 2512 | power resistors/caps, high current | large |

For a given value, prefer the smallest package whose power/voltage rating fits — and prefer the
one with both good stock and low price (the search tools already rank this way).

---

## Quick Reference: Position Estimation

For placing components, common canvas sizes:
- Default schematic: ~4000x3000 px
- Center of canvas: ~(2000, 1500)
- Component spacing: ~200-400 px apart
- Wire pin length: ~100 px from component body
