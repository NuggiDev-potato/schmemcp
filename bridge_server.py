import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import websockets
from mcp.server.mcpserver import MCPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("easyeda_mcp")

mcp = MCPServer(name="EasyEDA Standard Bridge")

_browser_connections: Dict[str, websockets.WebSocketServerProtocol] = {}
_pending_requests: Dict[str, asyncio.Future] = {}


def _generate_req_id() -> str:
    return uuid.uuid4().hex[:12]


async def _send_to_browser(action: str, args: Dict[str, Any]) -> Any:
    if not _browser_connections:
        raise ConnectionError("EasyEDA browser tab not connected via WebSocket.")

    conn_id = next(iter(_browser_connections))
    ws = _browser_connections[conn_id]
    req_id = _generate_req_id()

    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_requests[req_id] = future

    payload = json.dumps({
        "req_id": req_id,
        "action": action,
        "args": args,
    })

    await ws.send(payload)
    logger.info("Sent to browser: action=%s req_id=%s", action, req_id)

    try:
        result = await asyncio.wait_for(future, timeout=60.0)
    except asyncio.TimeoutError:
        _pending_requests.pop(req_id, None)
        raise TimeoutError(f"Timed out waiting for browser response (req_id={req_id})")

    return result


async def _handle_ws_message(conn_id: str, message: str) -> None:
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON from browser: %s", message[:200])
        return

    req_id = data.get("req_id")
    if req_id and req_id in _pending_requests:
        future = _pending_requests.pop(req_id)
        if not future.done():
            if data.get("status") == "success":
                future.set_result(data.get("data"))
            else:
                error_msg = data.get("error", data.get("message", "Unknown browser error"))
                future.set_exception(RuntimeError(error_msg))


async def _ws_handler(ws: websockets.WebSocketServerProtocol) -> None:
    conn_id = _generate_req_id()
    _browser_connections[conn_id] = ws
    logger.info("Browser connected: %s", conn_id)

    try:
        async for message in ws:
            await _handle_ws_message(conn_id, message)
    except websockets.exceptions.ConnectionClosed:
        logger.info("Browser disconnected: %s", conn_id)
    finally:
        _browser_connections.pop(conn_id, None)
        for req_id, future in list(_pending_requests.items()):
            if not future.done():
                future.set_exception(ConnectionError("Browser disconnected"))
                _pending_requests.pop(req_id, None)


@asynccontextmanager
async def _lifespan(server: MCPServer):
    _ws_server = await websockets.serve(_ws_handler, "localhost", 3579)
    logger.info("WebSocket server listening on ws://localhost:3579")
    try:
        yield
    finally:
        _ws_server.close()
        await _ws_server.wait_closed()


# ---------------------------------------------------------------------------
# Python-native LCSC search (no browser connection required)
# ---------------------------------------------------------------------------

_LCSC_SEARCH_URL = "https://easyeda.com/api/components/search"
_LCSC_PRODUCT_URL = "https://www.lcsc.com/product-detail/{}.html"
_LCSC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.lcsc.com/",
}

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)

# Parts considered unsuitable for generic use (removed from capacitor results).
_UNRECOMMENDED_DIELECTRICS = ("Y5V", "Z5U")


def _lcsc_search(query: str, max_items: int = 30) -> list:
    """Query the EasyEDA component search API and return LCSC library items."""
    import requests
    resp = requests.post(
        _LCSC_SEARCH_URL,
        json={"wd": query},
        headers=_LCSC_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    lists = (data.get("result") or {}).get("lists") or {}
    raw = lists.get("lcsc") or []
    items = []
    for item in raw[:max_items]:
        normalized = _normalize_search_item(item)
        if normalized and normalized.get("lcsc_id"):
            items.append(normalized)
    return items


def _normalize_search_item(item: dict) -> Dict[str, Any]:
    """Extract the fields we care about from one EasyEDA search result item."""
    head = ((item.get("dataStr") or {}).get("head") or {})
    cpara = head.get("c_para") or {}
    lcsc = item.get("lcsc") or {}
    return {
        "lcsc_id": lcsc.get("number"),
        "uuid": item.get("uuid") or item.get("datastrid"),
        "title": item.get("title"),
        "description": item.get("description"),
        "tags": item.get("tags"),
        "value": cpara.get("Value"),
        "package": cpara.get("package") or item.get("packageDetail"),
        "manufacturer": cpara.get("Manufacturer"),
        "mfr_part": cpara.get("Manufacturer Part"),
        "jlc_class": cpara.get("JLCPCB Part Class"),
        "smt": bool(item.get("SMT")),
        "jlc_on_sale": item.get("jlcOnSale") == 1,
    }


def _lcsc_stock_price(lcsc_id: str) -> Optional[Dict[str, Any]]:
    """Fetch real-time stock + first-tier price for one LCSC part from the product page.
    Returns None when the part has no LCSC product page (404 / no webData)."""
    import requests
    resp = requests.get(
        _LCSC_PRODUCT_URL.format(lcsc_id),
        headers=_LCSC_HEADERS,
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    m = _NEXT_DATA_RE.search(resp.text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    page_props = (data.get("props") or {}).get("pageProps") or {}
    web = page_props.get("webData") or {}
    price_list = web.get("productPriceList") or []
    price = None
    if price_list:
        try:
            price = float((price_list[0] or {}).get("currencyPrice"))
        except (TypeError, ValueError):
            price = None
    return {
        "stock": int(web.get("stockNumber") or 0),
        "price": price,
        "product_name": web.get("productNameEn"),
        "footprint": web.get("encapStandard"),
        "brand": web.get("brandNameEn"),
        "model": web.get("productModel"),
    }


async def _search_and_rank(
    features: list,
    max_results: int = 5,
    max_price: float = 5.0,
    component_type: Optional[str] = None,
    forbid_terms: tuple = (),
) -> Dict[str, Any]:
    terms = [str(f) for f in (features or []) if str(f).strip()]
    query = " ".join(terms)
    if component_type:
        query = f"{query} {component_type}".strip()

    items = await asyncio.to_thread(_lcsc_search, query)
    if not items:
        return {"candidates": [], "attempted": True, "query": query}

    stock_prices = await asyncio.gather(
        *(asyncio.to_thread(_lcsc_stock_price, it["lcsc_id"]) for it in items),
        return_exceptions=True,
    )

    candidates = []
    for item, stock_price in zip(items, stock_prices):
        if not isinstance(stock_price, dict) or not stock_price:
            continue
        if stock_price["stock"] is None or stock_price["stock"] <= 0:
            continue
        record = dict(item)
        record.update(stock_price)

        haystack = " ".join(str(v or "") for v in
                            (record.get("title"), record.get("description"),
                             record.get("tags"), record.get("value"),
                             record.get("product_name"), record.get("mfr_part"))).lower()
        if terms and not all(t.lower() in haystack for t in terms):
            continue
        if forbid_terms:
            if any(term.upper() in haystack.upper() for term in forbid_terms):
                continue
        if component_type:
            if component_type not in haystack:
                continue
        candidates.append(record)

    for c in candidates:
        if c.get("price") is None:
            c["price"] = float("inf")
    candidates.sort(key=lambda c: c["price"])
    candidates = candidates[:max_results]

    result = {
        "query": query,
        "attempted": True,
        "max_price": max_price,
        "tie": False,
        "candidates": [],
    }
    for c in candidates:
        price = c.get("price")
        result["candidates"].append({
            "lcsc_id": c.get("lcsc_id"),
            "title": c.get("product_name") or c.get("title"),
            "value": c.get("value"),
            "package": c.get("footprint") or c.get("package"),
            "manufacturer": c.get("brand") or c.get("manufacturer"),
            "mfr_part": c.get("model") or c.get("mfr_part"),
            "stock": c.get("stock"),
            "price": None if price == float("inf") else price,
            "over_budget": price != float("inf") and price > max_price,
            "jlc_class": c.get("jlc_class"),
            "smt": c.get("smt"),
        })
    top = result["candidates"]
    if len(top) >= 2:
        p0 = top[0].get("price")
        p1 = top[1].get("price")
        if p0 is not None and p1 is not None and abs(p0 - p1) < 1e-6:
            result["tie"] = True
    return result


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def place_component(lcsc_id: str, x: float, y: float) -> str:
    """Place an LCSC component on the EasyEDA schematic canvas.

    Args:
        lcsc_id: LCSC part number, e.g. "C123302".
        x: X-coordinate in EasyEDA internal pixels (1 px = 10 mil = 0.254 mm).
        y: Y-coordinate in EasyEDA internal pixels (1 px = 10 mil = 0.254 mm).
    """
    result = await _send_to_browser("PLACE_LCSC", {
        "lcscPartNumber": lcsc_id,
        "x": x,
        "y": y,
    })
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def eval_browser_js(code: str) -> str:
    """Execute JavaScript in the EasyEDA browser context. Use for probing APIs and debugging.

    Args:
        code: JavaScript code to execute.
    """
    result = await _send_to_browser("EXEC_JS", {"code": code})
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def get_canvas_source(type: str = "json") -> str:
    """Retrieve the full active EasyEDA document.

    Args:
        type: Document format - "json" (default), "compress", or "svg".
    """
    result = await _send_to_browser("GET_SOURCE", {"type": type})
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def search_lcsc_component(query: str) -> str:
    """Search the LCSC/EasyEDA parts database by keyword or C-number. Runs entirely in
    Python (no browser connection needed). Removes unavailable parts, keeps only real
    matches, and returns the top 5 by stock and price.

    Args:
        query: Search term (keyword like "NE555" or C-number like "C123302").
    """
    result = await _search_and_rank([query], max_results=5)
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def search_component(features: list, max_results: int = 5) -> str:
    """Search LCSC for any component described by a list of feature keywords
    (e.g. ["boost converter", "600 kHz"], ["I2C level shifter"], ["100nF", "0603"]).
    Runs entirely in Python. Removes out-of-stock and unrelated parts, keeps only parts
    matching ALL feature terms, sorts by price, returns top 5.

    Args:
        features: List of feature strings to match (all must appear in a result).
        max_results: Max candidates to return (1-5, default 5).
    """
    result = await _search_and_rank(features, max_results=max_results)
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def search_capacitor(features: list, max_results: int = 5) -> str:
    """Search LCSC for a capacitor matching a list of features (value, package, voltage,
    dielectric). Y5V and Z5U dielectrics are excluded by default: they lose most of their
    capacitance under DC bias and temperature changes (up to -80%/+30% over -30C..85C, and
    up to -50% at rated voltage), making them unsuitable for decoupling or filtering.
    Runs entirely in Python.

    Args:
        features: List of capacitor feature strings (e.g. ["100nF", "0603", "X7R"]).
        max_results: Max candidates to return (1-5, default 5).
    """
    result = await _search_and_rank(
        features,
        max_results=max_results,
        component_type="capacitor",
        forbid_terms=_UNRECOMMENDED_DIELECTRICS,
    )
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def search_resistor(features: list, max_results: int = 5) -> str:
    """Search LCSC for a resistor matching a list of features (value, package, tolerance,
    wattage). Runs entirely in Python.

    Args:
        features: List of resistor feature strings (e.g. ["10k", "0402", "1%"]).
        max_results: Max candidates to return (1-5, default 5).
    """
    result = await _search_and_rank(
        features,
        max_results=max_results,
        component_type="resistor",
    )
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def search_diode(features: list, max_results: int = 5) -> str:
    """Search LCSC for a diode matching a list of features (type, package, voltage/current
    ratings). Runs entirely in Python.

    Args:
        features: List of diode feature strings (e.g. ["1N4148", "SOD-323"], ["schottky", "3A", "40V"]).
        max_results: Max candidates to return (1-5, default 5).
    """
    result = await _search_and_rank(
        features,
        max_results=max_results,
        component_type="diode",
    )
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def search_inductor(features: list, max_results: int = 5) -> str:
    """Search LCSC for an inductor matching a list of features (value, package,
    current/sat current, frequency). Runs entirely in Python.

    Args:
        features: List of inductor feature strings (e.g. ["10uH", "2A", "1210"]).
        max_results: Max candidates to return (1-5, default 5).
    """
    result = await _search_and_rank(
        features,
        max_results=max_results,
        component_type="inductor",
    )
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def add_wire(x1: float, y1: float, x2: float, y2: float) -> str:
    """Draw a schematic wire between two points.

    Args:
        x1: Start X-coordinate in EasyEDA internal pixels (1 px = 10 mil = 0.254 mm).
        y1: Start Y-coordinate.
        x2: End X-coordinate.
        y2: End Y-coordinate.
    """
    result = await _send_to_browser("ADD_WIRE", {
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
    })
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def add_line(x1: float, y1: float, x2: float, y2: float,
                   stroke_color: str = "#00FF00", stroke_width: float = 1) -> str:
    """Draw a real line object on the EasyEDA schematic canvas.

    Args:
        x1: Start X-coordinate in EasyEDA internal pixels (1 px = 10 mil = 0.254 mm).
        y1: Start Y-coordinate.
        x2: End X-coordinate.
        y2: End Y-coordinate.
        stroke_color: Line color as hex string, e.g. "#000000".
        stroke_width: Line width in internal pixels, e.g. 1.
    """
    result = await _send_to_browser("ADD_LINE", {
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "strokeColor": stroke_color,
        "strokeWidth": stroke_width,
    })
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def update_net_name(gid: str, net_name: str) -> str:
    """Update the net assignment of a pad or track element.

    Args:
        gid: The gId of the pad/track element (e.g. "gge233_1").
        net_name: New net name to assign (e.g. "VCC", "GND", "3V3").
    """
    result = await _send_to_browser("UPDATE_NET_NAME", {
        "gid": gid,
        "net_name": net_name,
    })
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def delete_component(gids: list) -> str:
    """Delete one or more shapes/components by their gIds.

    Args:
        gids: List of gIds to delete, e.g. ["gge2", "gge3"].
    """
    result = await _send_to_browser("DELETE", {"gids": gids})
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def move_component(gids: list, add_x: float = 0, add_y: float = 0) -> str:
    """Move shapes in relative coordinates, like pressing the arrow keys.

    Args:
        gids: List of gIds to move, e.g. ["gge2", "gge3"].
        add_x: Relative X displacement in internal pixels (positive = right).
        add_y: Relative Y displacement in internal pixels (positive = down).
    """
    result = await _send_to_browser("MOVE_OBJS", {
        "gids": gids,
        "addX": add_x,
        "addY": add_y,
    })
    return json.dumps({"status": "success", "data": result})


@mcp.tool()
async def move_component_to(gids: list, x: float, y: float) -> str:
    """Move shapes to an absolute canvas position (coordinates are relative to the origin).

    Args:
        gids: List of gIds to move, e.g. ["gge2", "gge3"].
        x: Target X-coordinate in internal pixels.
        y: Target Y-coordinate in internal pixels.
    """
    result = await _send_to_browser("MOVE_OBJS_TO", {
        "gids": gids,
        "x": x,
        "y": y,
    })
    return json.dumps({"status": "success", "data": result})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_standalone() -> None:
    """Run only the WebSocket server."""
    logger.info("Starting standalone WebSocket bridge on ws://0.0.0.0:3579")
    server = await websockets.serve(_ws_handler, "0.0.0.0", 3579)
    logger.info("WebSocket server ready. Press Ctrl+C to stop.")
    try:
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        server.close()
        await server.wait_closed()


async def run_mcp() -> None:
    """Run the full MCP server with WebSocket bridge."""
    logger.info("Starting EasyEDA Standard MCP Bridge Server...")
    async with _lifespan(mcp):
        await mcp.run_stdio_async()


def main() -> None:
    import sys
    if "--standalone" in sys.argv:
        asyncio.run(run_standalone())
    else:
        asyncio.run(run_mcp())


if __name__ == "__main__":
    main()
