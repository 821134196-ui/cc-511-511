/* 公共前端工具 */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

async function requireLogin() {
  try {
    return await api("/api/me");
  } catch (e) {
    location.href = "/";
    throw e;
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(msg, kind = "") {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.className = `show ${kind}`;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.className = ""), 3200);
}

const PASS_STATUS = { active: "有效", suspended: "冻结待评估", revoked: "已撤销" };
function passChip(p) {
  const cls = p.status === "active" ? "ok" : p.status === "suspended" ? "warn" : "bad";
  const inside = p.inside && p.status === "active" ? " · 在区内" : "";
  return `<span class="chip ${cls}">${PASS_STATUS[p.status]}${inside}</span>`;
}

function appChip(s) {
  const map = { pending: ["warn", "待审批"], approved: ["ok", "已批准"], rejected: ["bad", "已拒绝"] };
  const [cls, txt] = map[s] || ["mute", s];
  return `<span class="chip ${cls}">${txt}</span>`;
}

/* 顶栏：按角色给导航 */
const NAVS = {
  nurse: [["/nurse", "护士站"], ["/board", "交接看板"], ["/records", "通行与撤销"]],
  head_nurse: [["/head", "审批发证"], ["/nurse", "病区床位"], ["/board", "交接看板"], ["/records", "通行与撤销"]],
  gate: [["/gate", "门岗核验"], ["/board", "交接看板"], ["/records", "通行记录"]],
};
const ROLE_NAME = { nurse: "护士", head_nurse: "护士长", gate: "门岗" };

async function renderNav(active) {
  const me = await requireLogin();
  const bar = document.getElementById("topbar");
  const links = NAVS[me.role].map(([href, txt]) =>
    `<a href="${href}" class="${href === active ? "active" : ""}">${txt}</a>`).join("");
  bar.innerHTML = `
    <div class="brand">病区陪护通行码</div>
    <nav>${links}</nav>
    <span class="who"><b>${esc(me.display_name)}</b> · ${ROLE_NAME[me.role]}${me.ward_name ? " · " + esc(me.ward_name) : me.gate_name ? " · " + esc(me.gate_name) + "门岗" : ""}</span>
    <button class="link" id="logoutBtn">退出</button>`;
  document.getElementById("logoutBtn").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/";
  };
  return me;
}

/* 遮罩值 + 点击显示完整值（敏感信息默认遮挡） */
function maskedField(masked, full) {
  const id = "m" + Math.random().toString(36).slice(2, 9);
  return `<span class="masked" id="${id}" data-m="${esc(masked)}" data-f="${esc(full)}">${esc(masked)}</span>
    <button class="reveal" type="button" onclick="toggleMask('${id}',this)">显示</button>`;
}
function toggleMask(id, btn) {
  const el = document.getElementById(id);
  const showing = el.textContent === el.dataset.f;
  el.textContent = showing ? el.dataset.m : el.dataset.f;
  btn.textContent = showing ? "显示" : "隐藏";
}
