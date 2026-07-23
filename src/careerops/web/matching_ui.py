"""Thin server-rendered UI for M2: match reports and remote eligibility."""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["matching-ui"])

_MATCH_REPORT_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Match Report - CareerOps</title></head>
<body>
<h1>匹配报告</h1>
<p>候选人: {candidate_name}</p>
<p>职位: {job_title}</p>
<p>层级: {tier}</p>
<p>总分: {score}</p>
<p>地域限制: {geo_blocked}</p>
<p>远程资格: {remote_verdict}</p>
<h2>需求匹配</h2>
<table border="1">
<tr><th>需求</th><th>级别</th><th>置信度</th><th>原因</th></tr>
{rows}
</table>
</body>
</html>
"""

_MATCHES_LIST_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Matches - CareerOps</title></head>
<body>
<h1>匹配结果</h1>
<table border="1">
<tr><th>职位</th><th>层级</th><th>分数</th><th>地域限制</th><th>远程资格</th></tr>
{rows}
</table>
</body>
</html>
"""


@router.get("/matches", response_class=HTMLResponse, include_in_schema=False)
async def matches_page(request: Request, response: Response) -> HTMLResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_matching_repository(request)
    if repo is None:
        html = _MATCHES_LIST_TEMPLATE.format(rows="<tr><td colspan='5'>暂无数据</td></tr>")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    matches = repo.list_matches(limit=100)
    rows = ""
    for m in matches:
        geo = "是" if m.get("geographic_blocked") else "否"
        rows += (
            f"<tr><td>{m['canonical_job_id']}</td>"
            f"<td>{m['tier']}</td>"
            f"<td>{m['overall_score']:.2f}</td>"
            f"<td>{geo}</td>"
            f"<td>{m.get('remote_verdict', 'unknown')}</td></tr>\n"
        )
    if not rows:
        rows = "<tr><td colspan='5'>暂无匹配结果</td></tr>"
    return HTMLResponse(_MATCHES_LIST_TEMPLATE.format(rows=rows))


@router.get("/matches/{match_id}", response_class=HTMLResponse, include_in_schema=False)
async def match_detail_page(match_id: str, request: Request, response: Response) -> HTMLResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_matching_repository(request)
    if repo is None:
        return HTMLResponse("<html><body><h1>服务不可用</h1></body></html>", status_code=503)
    detail = repo.get_match_detail(match_id)
    if detail is None:
        return HTMLResponse("<html><body><h1>未找到</h1></body></html>", status_code=404)
    rows = ""
    for rm in detail.get("requirement_matches", []):
        rows += (
            f"<tr><td>{rm['requirement_name']}</td>"
            f"<td>{rm['level']}</td>"
            f"<td>{rm.get('confidence', 0):.2f}</td>"
            f"<td>{rm.get('reason', '')}</td></tr>\n"
        )
    geo = "是" if detail.get("geographic_blocked") else "否"
    html = _MATCH_REPORT_TEMPLATE.format(
        candidate_name=detail.get("candidate_name", ""),
        job_title=detail.get("job_title", ""),
        tier=detail.get("tier", ""),
        score=f"{detail.get('overall_score', 0):.2f}",
        geo_blocked=geo,
        remote_verdict=detail.get("remote_verdict", "unknown"),
        rows=rows,
    )
    return HTMLResponse(html)


def _get_matching_repository(request: Request) -> Any:
    return getattr(request.app.state, "matching_repository", None)
