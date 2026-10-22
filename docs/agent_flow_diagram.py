"""Re-cat LangGraph 에이전트 노드 흐름 다이어그램.
재생성: pip install matplotlib && python docs/agent_flow_diagram.py
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Ellipse, Circle

for name in ["AppleGothic", "Apple SD Gothic Neo", "Malgun Gothic", "NanumGothic"]:
    try:
        fm.findfont(name, fallback_to_default=False)
        plt.rcParams["font.family"] = name
        break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False

W, H = 17.5, 11.6
fig, ax = plt.subplots(figsize=(W, H), dpi=200)
ax.set_xlim(0, W)
ax.set_ylim(0, H)
ax.axis("off")
fig.patch.set_facecolor("white")

EDGE = "#5B6472"
NODE = ("#FFFFFF", "#2F6FB0", "#133A5C")
TERM = ("#F4F6F8", "#8A93A6", "#2B3340")
TOP = ("#F4F6F8", "#8A93A6", "#2B3340")
AGENT_HDR = ("#E8F1FC", "#3B82C4", "#133A5C")
EXT = ("#F3ECFB", "#8450C9", "#3A1E66")
PANEL_BG, PANEL_EDGE = "#FBF8EE", "#E3D9B6"


def rect(x, y, w, h, text, style=NODE, fontsize=10, highlight=False, num=None):
    fill, edge, tc = style
    patch = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                            linewidth=2.6 if highlight else 1.5,
                            edgecolor="#D64545" if highlight else edge, facecolor=fill, zorder=3)
    ax.add_patch(patch)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fontsize,
             fontweight="bold", color=tc, zorder=4)
    if num is not None:
        badge(x + w - 0.03, y + h - 0.03, num)
    return (x, y, w, h)


def oval(x, y, w, h, text, style=NODE, fontsize=9.5):
    fill, edge, tc = style
    patch = Ellipse((x + w / 2, y + h / 2), w, h, linewidth=1.4, edgecolor=edge, facecolor=fill, zorder=3)
    ax.add_patch(patch)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fontsize,
             fontweight="bold", color=tc, zorder=4)
    return (x, y, w, h)


def badge(cx, cy, n):
    ax.add_patch(Circle((cx, cy), 0.17, facecolor="#D64545", edgecolor="white", linewidth=1.2, zorder=6))
    ax.text(cx, cy, str(n), ha="center", va="center", fontsize=9, fontweight="bold", color="white", zorder=7)


def anchor(b, side):
    x, y, w, h = b
    return {"top": (x + w / 2, y + h), "bottom": (x + w / 2, y), "left": (x, y + h / 2), "right": (x + w, y + h / 2)}[side]


def arrow(b1, s1, b2, s2, label=None, color=EDGE, curve=0.0, lw=1.7, lpos=0.5, loffset=(0, 0.2)):
    p1, p2 = anchor(b1, s1), anchor(b2, s2)
    a = FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=14, color=color, linewidth=lw,
                         connectionstyle=f"arc3,rad={curve}", zorder=2)
    ax.add_patch(a)
    if label:
        mx, my = p1[0] + (p2[0] - p1[0]) * lpos, p1[1] + (p2[1] - p1[1]) * lpos
        if curve:
            mx += (p1[1] - p2[1]) * curve * 0.5
            my += (p2[0] - p1[0]) * curve * 0.5
        ax.text(mx + loffset[0], my + loffset[1], label, fontsize=8.2, color="#3B4252", ha="center", va="center",
                zorder=5, bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="#D7DBE1", alpha=0.95))


def elbow(b1, s1, b2, s2, mid_y, label=None, lw=1.7, color=EDGE):
    """직선 경로가 다른 노드를 가로지를 때 쓰는 ㄱ자(수직-수평-수직) 경로."""
    p1, p2 = anchor(b1, s1), anchor(b2, s2)
    ax.plot([p1[0], p1[0]], [p1[1], mid_y], color=color, linewidth=lw, zorder=2)
    ax.plot([p1[0], p2[0]], [mid_y, mid_y], color=color, linewidth=lw, zorder=2)
    a = FancyArrowPatch((p2[0], mid_y), p2, arrowstyle="-|>", mutation_scale=14, color=color, linewidth=lw, zorder=2)
    ax.add_patch(a)
    if label:
        ax.text((p1[0] + p2[0]) / 2, mid_y + 0.2, label, fontsize=8.2, color="#3B4252", ha="center", va="center",
                zorder=5, bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="#D7DBE1", alpha=0.95))


# ================= 제목 =================
ax.text(0.3, H - 0.42, "Re-cat 에이전트 그래프 (LangGraph) — 실제 질문 예시로 본 흐름", fontsize=17, fontweight="bold", color="#13233A")
ax.text(0.3, H - 0.76, "빨간 번호는 오른쪽 \"실제 질문 예시\" 패널과 대응한다. 빨간 테두리는 이번 작업에서 가장 깊게 다룬 분기점.", fontsize=9.6, color="#5B6472")

# ================= 상단: Client / Backend / Agent =================
top_y = H - 1.72
client_b = rect(0.4, top_y, 2.6, 0.66, "Client (Browser)", TOP, fontsize=10)
backend_b = rect(3.3, top_y, 2.9, 0.66, "Backend (FastAPI :8100)", TOP, fontsize=10)
agent_b = rect(6.5, top_y, 5.7, 0.66, "Agent (LangGraph)", AGENT_HDR, fontsize=11)
mcp_b = rect(12.5, top_y, 1.9, 0.66, "MCP\n정적 분석", EXT, fontsize=9.3)
ollama_b = rect(14.7, top_y, 2.0, 0.66, "Ollama\n분류·설명", EXT, fontsize=9.3)

arrow(client_b, "right", backend_b, "left")
arrow(backend_b, "left", client_b, "right", curve=0.3)
arrow(backend_b, "right", agent_b, "left")
arrow(agent_b, "left", backend_b, "right", curve=0.3)
arrow(agent_b, "right", mcp_b, "left", curve=-0.25, color="#8450C9")
arrow(agent_b, "right", ollama_b, "left", curve=-0.35, color="#1F8A4C")

ax.text(W / 2, top_y - 0.22, "MCP → get_callees/get_chain/search_docs 등 정적 분석 결과 제공   ·   Ollama → 질문 분류(route)·탐색 선택·호출 흐름 설명 생성",
        fontsize=8.6, color="#5B6472", ha="center")

# ================= 그래프 컨테이너 (배경은 투명 — 안의 화살표를 가리면 안 됨) =================
cont_top = top_y - 0.85
ax.add_patch(FancyBboxPatch((0.3, 0.3), 14.0, cont_top - 0.3, boxstyle="round,pad=0.02,rounding_size=0.08",
                             linewidth=1.5, edgecolor=NODE[1], facecolor="none", zorder=0))

gate_b = rect(1.3, cont_top - 0.95, 1.6, 0.62, "gate", NODE, fontsize=10)
route_b = rect(3.3, cont_top - 0.95, 1.8, 0.62, "route", NODE, fontsize=10.5, highlight=True, num=1)
arrow(gate_b, "right", route_b, "left")

fork_y = cont_top - 1.5          # route 아래, 세 갈래로 갈라지기 전 공용 높이
locate_top_y = cont_top - 2.35   # 코드/문서 경로 첫 노드가 시작되는 높이 (route보다 한 칸 아래)

# ---- 가지 1: 잡담 (route 바로 아래, 왼쪽) ----
chat_b = rect(0.6, fork_y - 0.65, 1.9, 0.6, "chat_answer", NODE, fontsize=9.3)
end1_b = oval(0.8, fork_y - 1.55, 1.15, 0.72, "END", TERM, fontsize=9)
arrow(route_b, "bottom", chat_b, "top", curve=0.35, label="잡담")
arrow(chat_b, "bottom", end1_b, "top")

# ---- 가지 2: 코드 질문 경로 (route 바로 아래) ----
locate_b = rect(3.3, locate_top_y, 1.7, 0.6, "locate", NODE, fontsize=9.5, num=2)
explore_b = rect(3.3, locate_top_y - 0.95, 1.7, 0.6, "explore", NODE, fontsize=9.5)
explain_b = rect(3.3, locate_top_y - 1.9, 1.7, 0.6, "explain", NODE, fontsize=9.5)
verify_b = rect(3.3, locate_top_y - 2.85, 1.7, 0.6, "verify", NODE, fontsize=9.5, num=3)
fallback_b = rect(5.4, locate_top_y - 2.85, 1.7, 0.6, "fallback", NODE, fontsize=9.0)
finalize_b = rect(3.3, locate_top_y - 3.8, 1.9, 0.6, "finalize", NODE, fontsize=9.5)
end2_b = oval(5.55, locate_top_y - 3.87, 1.15, 0.72, "END", TERM, fontsize=9)

arrow(route_b, "bottom", locate_b, "top", label="이름/기능 질문")
arrow(locate_b, "bottom", explore_b, "top")
arrow(explore_b, "bottom", explain_b, "top")
arrow(explain_b, "bottom", verify_b, "top")
arrow(verify_b, "left", explain_b, "left", curve=-0.6, label="재설명(최대 2회)", loffset=(-0.55, 0))
arrow(verify_b, "right", fallback_b, "left", label="끝내 실패", loffset=(0, 0.32))
arrow(verify_b, "bottom", finalize_b, "top", label="통과")
arrow(fallback_b, "bottom", finalize_b, "right", curve=0.3)
arrow(finalize_b, "right", end2_b, "left")

# ---- 가지 3: 문서 질문 경로 (route에서 오른쪽으로 꺾어 들어감) ----
doc_x = 9.6
doc_locate_b = rect(doc_x, locate_top_y, 1.95, 0.6, "doc_locate", NODE, fontsize=9.2, num=4)
doc_check_b = rect(doc_x, locate_top_y - 0.95, 1.95, 0.6, "doc_check", NODE, fontsize=9.2, num=5)
doc_explain_b = rect(doc_x, locate_top_y - 1.9, 1.95, 0.6, "doc_explain", NODE, fontsize=9.2)
doc_verify_b = rect(doc_x, locate_top_y - 2.85, 1.95, 0.6, "doc_verify", NODE, fontsize=9.2)
finalize2_b = rect(doc_x, locate_top_y - 3.8, 1.95, 0.6, "finalize", NODE, fontsize=9.5)
end3_b = oval(doc_x + 2.25, locate_top_y - 3.87, 1.15, 0.72, "END", TERM, fontsize=9)

elbow(route_b, "right", doc_locate_b, "top", mid_y=fork_y, label="문서 질문")
arrow(doc_locate_b, "bottom", doc_check_b, "top")
arrow(doc_check_b, "bottom", doc_explain_b, "top")
arrow(doc_explain_b, "bottom", doc_verify_b, "top")
arrow(doc_verify_b, "bottom", finalize2_b, "top")
arrow(finalize2_b, "right", end3_b, "left")
ax.text(doc_x + 1.95 + 0.12, locate_top_y + 0.3, "맞는 절 없으면\n바로 finalize로", fontsize=7.6, color="#5B6472",
        ha="left", va="center", linespacing=1.3)

# ================= 범례 =================
lx, ly = 0.5, 0.05
for style, label in [(NODE, "처리 노드"), (TERM, "종료/보조"), (EXT, "외부 연동")]:
    fill, edge, _ = style
    ax.add_patch(FancyBboxPatch((lx, ly), 0.28, 0.16, boxstyle="round,pad=0.01,rounding_size=0.03",
                                 linewidth=1.0, edgecolor=edge, facecolor=fill, zorder=8))
    ax.text(lx + 0.37, ly + 0.08, label, fontsize=8.3, va="center", color="#3B4252")
    lx += 0.37 + len(label) * 0.105 + 0.45
ax.add_patch(FancyBboxPatch((lx, ly), 0.28, 0.16, boxstyle="round,pad=0.01,rounding_size=0.03",
                             linewidth=2.2, edgecolor="#D64545", facecolor="white", zorder=8))
ax.text(lx + 0.37, ly + 0.08, "핵심 분기점", fontsize=8.3, va="center", color="#3B4252")

# ================= 오른쪽 패널: 실제 질문 예시 =================
px, py, pw, ph = 14.6, 0.3, 2.6, cont_top - 0.3
ax.add_patch(FancyBboxPatch((px, py), pw, ph, boxstyle="round,pad=0.02,rounding_size=0.08",
                             linewidth=1.2, edgecolor=PANEL_EDGE, facecolor=PANEL_BG, zorder=2))
ax.text(px + pw / 2, py + ph - 0.32, "실제 질문 예시", fontsize=11, fontweight="bold", color="#5C4A12",
        ha="center", zorder=3)

items = [
    (1, "route", "\"_doc_files 호출 흐름\"\n규칙이 trace면 모델도\n규칙을 못 뒤집음 (수정)"),
    (2, "locate", "있는 이름 → 바로 시작\n없는 이름(PaymentGateway\n.charge) → 비슷한 이름 제안"),
    (3, "verify", "주장이 분석과 다르면\n재설명, 끝내 틀리면\n정적 분석 결과만 반환"),
    (4, "doc_locate", "\"트러블슈팅 전체 보여줘\"\n→ 절 하나가 아니라 문서\n전체를 모아 요약"),
    (5, "doc_check", "문서의 \"A→B\" 서술을 대조.\nLangGraph 노드 간선도\n인식해 검증 (수정)"),
]
iy = py + ph - 0.55
for n, node, text in items:
    badge(px + 0.32, iy, n)
    ax.text(px + 0.6, iy + 0.02, node, fontsize=9.3, fontweight="bold", color="#133A5C", va="center")
    iy -= 0.3
    ax.text(px + 0.18, iy, text, fontsize=7.5, color="#2B3340", va="top", linespacing=1.35)
    iy -= 0.27 * text.count("\n") + 0.5

plt.tight_layout()
out = Path(__file__).resolve().parent / "agent_flow.png"
fig.savefig(out, dpi=200, facecolor="white", bbox_inches="tight")
print("saved", out)

