"""Regression tests for parsing `go tool cover` output.

The line number matters beyond display: go_test_agent slices the function's source
using it, so a None here silently degrades test generation to "no source context".
"""
from src.integration import reports

# Real `go tool cover -func` output shape: TAB-separated, location ends with a trailing colon.
REAL = (
    "github.com/gorilla/mux/mux.go:238:\tGetRoute\t\t0.0%\n"
    "github.com/gorilla/mux/mux.go:354:\tMatcherFunc\t\t0.0%\n"
    "github.com/gorilla/mux/route.go:42:\tSkipClean\t\t85.7%\n"
    "total:\t\t\t\t(statements)\t\t90.7%\n"
)
# Some toolchains/wrappers emit line:col instead.
WITH_COL = "github.com/gorilla/mux/mux.go:238:17:\tGetRoute\t\t0.0%\n"


def test_trailing_colon_location_yields_a_real_line_number():
    out = reports.parse_cover_func(REAL)
    by = {f["func"]: f for f in out["funcs"]}
    assert by["GetRoute"]["line"] == 238
    assert by["GetRoute"]["file"] == "github.com/gorilla/mux/mux.go"
    assert by["SkipClean"]["line"] == 42
    assert all(f["line"] is not None for f in out["funcs"])


def test_line_and_column_location_keeps_the_line():
    f = reports.parse_cover_func(WITH_COL)["funcs"][0]
    assert (f["file"], f["line"]) == ("github.com/gorilla/mux/mux.go", 238)


def test_total_and_uncovered_still_parse():
    out = reports.parse_cover_func(REAL)
    assert out["total_pct"] == 90.7
    assert sorted(f["func"] for f in out["uncovered"]) == ["GetRoute", "MatcherFunc"]


def test_malformed_location_degrades_to_none_without_crashing():
    out = reports.parse_cover_func("weird-no-colon\tFn\t\t0.0%\n")
    assert out["funcs"][0]["line"] is None
