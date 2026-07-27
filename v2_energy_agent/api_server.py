"""电碳数据 API 服务（FastAPI）。

按日期返回湖南发电结构衍生的碳排放因子 C(τ)/Cr(τ) 与湖北实时出清电价，
供前端按日期切换、替换默认因子/电价。同时 serve 静态前端页面（同源避免 CORS）。

启动: python api_server.py [--port 8000]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from energy_agent_v2.algorithms.carbon_accounting import CarbonAccountant
from energy_agent_v2.contracts import EmissionFactorLibrary
from energy_agent_v2.data.csv_fetcher import CSVDataFetcher

BASE_DIR = Path(__file__).resolve().parent
_fetcher = CSVDataFetcher()
_accountant = CarbonAccountant()
_EF = EmissionFactorLibrary(
    source_label="national-grid-avg-2022", calibrated_region="cn-hunan"
)

app = FastAPI(title="园区电碳数据服务", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/v1/data-range")
def data_range() -> dict:
    """返回发电结构数据的可用日期范围。"""
    try:
        total = _fetcher._load_csv(_fetcher.hunan_dir / "实时总发电出力.csv")
        price = _fetcher._load_csv(_fetcher.price_path)
        gen_dates = sorted({ts.date().isoformat() for ts in total})
        price_dates = sorted({ts.date().isoformat() for ts in price})
    except Exception:
        return {"generation": None, "price": None}
    return {
        "generation": {"start": gen_dates[0], "end": gen_dates[-1], "days": len(gen_dates)},
        "price": {"start": price_dates[0], "end": price_dates[-1], "days": len(price_dates)},
    }


def _build_response(target: date) -> dict:
    """按目标日期计算电价 + 碳因子，返回前端可直接消费的 JSON。"""
    start = datetime.combine(target, time(0, 0))
    end = datetime.combine(target, time(23, 45))
    mix = _fetcher.fetch_generation_mix(start, end)
    raw_prices = _fetcher.fetch_realtime_price(start, end)
    if not mix or len(mix) != 96 or not raw_prices or len(raw_prices) != 96:
        raise HTTPException(
            status_code=404,
            detail=f"日期 {target.isoformat()} 无完整 96 点发电结构或电价数据",
        )
    actual_date = mix[0].timestamp.date().isoformat()
    factors = _accountant.compute_factors(mix, _EF, "cn-hunan")
    price_kwh = [round(p / 1000.0, 6) for p in raw_prices]
    c_factors = [round(v, 6) for v in factors.direct_factor_c_kg_per_kwh]
    cr_factors = [round(v, 6) for v in factors.responsibility_factor_cr_kg_per_kwh]
    time_labels = [f"{i * 15 // 60:02d}:{(i * 15) % 60:02d}" for i in range(96)]
    gen_mix = [
        {
            "coal": round(p.generation_by_source_kw.get("coal", 0.0), 1),
            "solar": round(p.generation_by_source_kw.get("solar", 0.0), 1),
            "wind": round(p.generation_by_source_kw.get("wind", 0.0), 1),
            "hydro": round(p.generation_by_source_kw.get("hydro", 0.0), 1),
            "purchase": round(p.generation_by_source_kw.get("purchase", 0.0), 1),
            "total": round(p.total_generation_kw, 1),
        }
        for p in mix
    ]

    def _stats(arr):
        return {
            "mean": round(sum(arr) / len(arr), 4),
            "max": round(max(arr), 4),
            "min": round(min(arr), 4),
        }

    return {
        "date": target.isoformat(),
        "actual_date": actual_date,
        "data_source": "csv-realtime",
        "points": 96,
        "time_labels": time_labels,
        "price": price_kwh,
        "c_factor": c_factors,
        "cr_factor": cr_factors,
        "gen_mix": gen_mix,
        "summary": {
            "price": _stats(price_kwh),
            "c_factor": _stats(c_factors),
            "ef_version": factors.emission_factor_library_version,
        },
    }


@app.get("/api/v1/grid-factors")
def grid_factors(date: str = Query(..., description="目标日期 YYYY-MM-DD")) -> dict:
    """按日期返回 96 点电价、碳因子 C(τ)/Cr(τ) 与发电结构。"""
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")
    return _build_response(target)


@app.get("/")
def index_page() -> FileResponse:
    """serve 前端主页面（同源，避免 CORS）。"""
    html = BASE_DIR / "index.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="index.html 未生成，请先运行 build_dashboard.py")
    return FileResponse(html, media_type="text/html")


app.mount("/static", StaticFiles(directory=str(BASE_DIR)), name="static")


def main() -> None:
    parser = argparse.ArgumentParser(description="电碳数据 API 服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    import uvicorn
    print(f"电碳数据服务启动中 -> http://{args.host}:{args.port}")
    print(f"  API:  /api/v1/grid-factors?date=YYYY-MM-DD")
    print(f"  页面: http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
