"""Re-cat 아키텍처 다이어그램 생성 스크립트.
재생성: pip install matplotlib && python docs/architecture_diagram.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

# ---- 한글 폰트 ----
for name in ["AppleGothic", "Apple SD Gothic Neo", "Malgun Gothic", "NanumGothic"]:
    try:
        fm.findfont(name, fallback_to_default=False)
        plt.rcParams["font.family"] = name
        break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False

FIG_W, FIG_H = 15.0, 10.4
fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), dpi=200)
ax.set_xlim(0, FIG_W)
ax.set_ylim(0, FIG_H)
ax.axis("off")
fig.patch.set_facecolor("white")

# ---- 색상 팔레트 ----
C = {
    "client": ("#F4F6F8", "#8A93A6", "#2B3340"),
    "frontend": ("#E8F1FC", "#3B82C4", "#133A5C"),
    "backend": ("#FFFFFF", "#2F6FB0", "#133A5C"),
    "code_path": ("#FFF4E5", "#D98324", "#5C3A09"),
    "doc_path": ("#EAF7EE", "#2E9E5B", "#0E4420"),
    "mcp": ("#F3ECFB", "#8450C9", "#3A1E66"),
    "ollama": ("#E8F8EE", "#1F9D55", "#0E4420"),
    "pg": ("#E8F0FE", "#2F6FB0", "#133A5C"),
    "edge": "#5B6472",
}


def box(x, y, w, h, text, key, fontsize=11.5, bold=True, sub=None, radius=0.07):
    fill, edge, textcolor = C[key]
    patch = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0.02,rounding_size={radius}",
                            linewidth=1.6, edgecolor=edge, facecolor=fill, zorder=2)
    ax.add_patch(patch)
    if sub:
        ax.text(x + w / 2, y + h / 2 + 0.14, text, ha="center", va="center",
                 fontsize=fontsize, fontweight="bold" if bold else "normal", color=textcolor, zorder=3)
        ax.text(x + w / 2, y + h / 2 - 0.20, sub, ha="center", va="center",
                 fontsize=fontsize - 2.3, color=textcolor, zorder=3)
    else:
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
                 fontsize=fontsize, fontweight="bold" if bold else "normal", color=textcolor, zorder=3)
    return (x, y, w, h)


def arrow(b1, b2, side1="bottom", side2="top", label=None, color=None, style="-|>", lw=1.8, curve=0.0):
    x1, y1, w1, h1 = b1
    x2, y2, w2, h2 = b2
    pts = {"bottom": (x1 + w1 / 2, y1), "top": (x1 + w1 / 2, y1 + h1),
           "left": (x1, y1 + h1 / 2), "right": (x1 + w1, y1 + h1 / 2)}
    pts2 = {"bottom": (x2 + w2 / 2, y2), "top": (x2 + w2 / 2, y2 + h2),
            "left": (x2, y2 + h2 / 2), "right": (x2 + w2, y2 + h2 / 2)}
    p1, p2 = pts[side1], pts2[side2]
    a = FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=15,
                         color=color or C["edge"], linewidth=lw, zorder=1,
                         connectionstyle=f"arc3,rad={curve}")
    ax.add_patch(a)
    if label:
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
        ax.text(mx + 0.18, my, label, fontsize=9, color="#3B4252", ha="left", va="center",
                zorder=4, bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.85))


# ================= 레이어 1: 클라이언트 =================
ax.text(0.3, FIG_H - 0.55, "Re-cat 시스템 아키텍처", fontsize=20, fontweight="bold", color="#13233A")
ax.text(0.3, FIG_H - 0.95, "온프레미스 · 단일 서버 PoC  —  코드/문서 질문 → 정적 분석 검증 → LLM 설명", fontsize=11, color="#5B6472")

user_b = box(0.6, FIG_H - 2.1, 3.0, 0.9, "사용자 화면", "client", sub="GET /  (web_chat.html)")
admin_b = box(4.0, FIG_H - 2.1, 3.0, 0.9, "관리자 화면", "client", sub="GET /admin  (web_admin.html)")

ax.text(0.6, FIG_H - 2.35, "Browser", fontsize=10.5, color="#8A93A6", style="italic")

# ================= 레이어 2: 프론트엔드 프록시 =================
fe_b = box(0.6, FIG_H - 3.5, 6.4, 0.9, "Frontend Proxy", "frontend",
           sub="frontend/server.py :8600  ·  /chat·/chat/stream(SSE) 프록시")
arrow(user_b, fe_b, "bottom", "top")
arrow(admin_b, fe_b, "bottom", "top")

# ================= 레이어 3: FastAPI 백엔드 (에이전트 그래프) =================
be_x, be_y, be_w, be_h = 0.6, FIG_H - 6.55, 13.8, 2.75
backend_outer = box(be_x, be_y, be_w, be_h, "", "backend", radius=0.09)
ax.text(be_x + 0.25, be_y + be_h - 0.3, "FastAPI Backend  :8100", fontsize=13, fontweight="bold", color="#133A5C")
ax.text(be_x + 0.25, be_y + be_h - 0.62, "LangGraph Agent — gate(보안 게이트) → route(질문 분류)", fontsize=9.6, color="#44536B")

arrow(fe_b, backend_outer, "bottom", "top")

# -- gate/route 칩 --
gr_b = box(be_x + 0.35, be_y + be_h - 1.35, 2.3, 0.55, "gate → route", "backend", fontsize=10, bold=False)

# -- 코드 경로 칩 --
code_y = be_y + 0.35
code_b = box(be_x + 0.35, code_y, 6.2, 0.95, "코드 질문 경로", "code_path", fontsize=11,
             sub="locate → explore → explain → verify → finalize")

# -- 문서 경로 칩 --
doc_b = box(be_x + 6.9, code_y, 6.2, 0.95, "문서 질문 경로", "doc_path", fontsize=11,
            sub="doc_locate → doc_check → doc_explain → doc_verify")

arrow(gr_b, code_b, "bottom", "top", curve=-0.15)
arrow(gr_b, doc_b, "bottom", "top", curve=0.15)

# ================= 레이어 4: 외부 서비스 =================
svc_y = 0.5
mcp_b = box(0.6, svc_y, 4.2, 1.5, "MCP Server", "mcp", sub="정적 분석: Python AST · Java tree-sitter\n(서브프로세스, 자동 복구)")
ollama_b = box(5.3, svc_y, 4.2, 1.5, "Ollama", "ollama", sub="로컬 LLM: fast · quality · embed\n(qwen3:0.6b/8b, bge-m3)")
pg_b = box(10.0, svc_y, 4.2, 1.5, "PostgreSQL + pgvector", "pg", sub="등록부 · 대화 · 감사 로그 · 문서 벡터")

arrow(code_b, mcp_b, "bottom", "top", label="get_callees / get_chain")
arrow(code_b, ollama_b, "bottom", "top", label="explain / verify 피드백", curve=0.08)
arrow(doc_b, mcp_b, "bottom", "top", curve=-0.12)
arrow(doc_b, ollama_b, "bottom", "top", label="doc_explain")
arrow(backend_outer, pg_b, "right", "top", label="등록부·대화·감사·임베딩", curve=-0.25)

# 범례
legend_items = [
    ("backend", "FastAPI 백엔드"), ("code_path", "코드 질문 경로"), ("doc_path", "문서 질문 경로"),
    ("mcp", "정적 분석 (MCP)"), ("ollama", "로컬 LLM (Ollama)"), ("pg", "영속 저장 (PostgreSQL)"),
]
lx = 0.6
for key, label in legend_items:
    fill, edge, _ = C[key]
    ax.add_patch(FancyBboxPatch((lx, FIG_H - 0.18), 0.28, 0.16, boxstyle="round,pad=0.01,rounding_size=0.03",
                                 linewidth=1.0, edgecolor=edge, facecolor=fill, zorder=5))
    ax.text(lx + 0.36, FIG_H - 0.10, label, fontsize=8.3, va="center", color="#3B4252")
    lx += 0.36 + len(label) * 0.085 + 0.35

plt.tight_layout()
out = Path(__file__).resolve().parent / "architecture.png"
fig.savefig(out, dpi=200, facecolor="white", bbox_inches="tight")
print("saved", out)
