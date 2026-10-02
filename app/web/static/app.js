/* 图书馆阅读挑战 —— 三端单页前端（无框架） */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  activities: [], people: { parents: [], librarians: [] }, children: [],
  view: "child",
  uid: { parent: null, librarian: null, child: null },
};

const api = {
  async get(p) {
    const r = await fetch(p);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "请求失败");
    return j;
  },
  async post(p, body = {}) {
    const r = await fetch(p, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "操作失败");
    return j;
  },
};

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  setTimeout(() => (t.className = "toast"), 2600);
}

const STATUS_CN = { draft: "草稿", submitted: "已提交", verified: "已核验", rejected: "需补充" };
const KIND_CN = { create: "创建", update: "修改", submit: "提交", review: "审核" };

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function ageOn(birth, on) {
  const b = new Date(birth), d = new Date(on);
  let a = d.getFullYear() - b.getFullYear();
  if (d.getMonth() < b.getMonth() || (d.getMonth() === b.getMonth() && d.getDate() < b.getDate())) a--;
  return a;
}

// ---------------------------------------------------------------- 初始化
async function boot() {
  [state.activities, state.people] = await Promise.all([
    api.get("/api/activities"),
    api.get("/api/people"),
  ]);
  state.children = await api.get("/api/children");

  fillSelect($("#parent-select"), state.people.parents, "name", "id");
  fillSelect($("#lib-select"), state.people.librarians, "name", "id");
  fillSelect($("#child-select"), state.children, "name", "id");
  fillSelect($("#lib-activity"), state.activities, "name", "id");

  state.uid.parent = +$("#parent-select").value;
  state.uid.librarian = +$("#lib-select").value;
  state.uid.child = +$("#child-select").value;

  $$(".role-btn").forEach(b => b.onclick = () => switchView(b.dataset.view));
  $("#parent-select").onchange = e => { state.uid.parent = +e.target.value; renderParent(); };
  $("#lib-select").onchange = e => { state.uid.librarian = +e.target.value; renderLibrarian(); };
  $("#child-select").onchange = e => { state.uid.child = +e.target.value; renderChild(); };
  $("#lib-activity").onchange = renderLibrarian;
  $("#btn-sync-loans").onclick = syncLoans;
  $("#btn-sync-loans-parent").onclick = syncLoans;
  $("#btn-restock").onclick = restock;
  $("#btn-pickup").onclick = pickup;
  $("#modal-close").onclick = () => $("#modal").classList.add("hidden");
  $("#modal").onclick = e => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

  renderChild();
}

function fillSelect(sel, items, labelKey, valKey) {
  sel.innerHTML = items.map(x =>
    `<option value="${x[valKey]}">${esc(x[labelKey])}</option>`).join("");
}

function switchView(v) {
  state.view = v;
  $$(".role-btn").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach(s => s.classList.toggle("active", s.id === "view-" + v));
  if (v === "child") renderChild();
  if (v === "parent") renderParent();
  if (v === "librarian") renderLibrarian();
}

async function syncLoans() {
  try {
    const r = await api.post("/api/loans/sync");
    toast(r.synced ? `已同步 ${r.synced} 条晚到的借阅记录` : "暂无待同步借阅", "ok");
  } catch (e) { toast(e.message, "err"); }
  if (state.view === "librarian") renderLibrarian();
  if (state.view === "parent") renderParent();
}

// ---------------------------------------------------------------- 孩子端
async function renderChild() {
  const child = state.children.find(c => c.id === state.uid.child);
  if (!child) return;
  const enrs = await api.get(`/api/enrollments?child_id=${child.id}`);
  $("#child-age").textContent = state.activities.map(a => {
    const e = enrs.find(x => x.activity_id === a.id);
    return e ? null : `${a.age_min}-${a.age_max}岁组`;
  }).filter(Boolean).length
    ? `可选年龄组见家长端`
    : `已报名 ${enrs.length} 个挑战`;

  if (!enrs.length) {
    $("#child-content").innerHTML = `<div class="card">还没有报名任何挑战，请家长在“家长端”报名。</div>`;
    return;
  }
  let html = "";
  for (const e of enrs) {
    const p = await api.get(`/api/progress?enrollment_id=${e.id}`);
    const pct = Math.min(100, Math.round((p.verified_count / p.goal) * 100));
    const act = state.activities.find(a => a.id === p.activity.id);
    html += `
      <div class="progress-ring-wrap">
        <div class="bar">
          <div class="bar-label">
            <b>${esc(act.name)}</b>
            <span>已核验 ${p.verified_count} / ${p.goal} 本</span>
          </div>
          <div class="track"><div class="fill ${pct === 100 ? "done" : ""}" style="width:${pct}%"></div>
          </div>
          <div class="qual-banner">${p.qualified
            ? (p.medal
                ? medalText(p.medal)
                : "🎉 达标啦！等待馆员审核发章")
            : `再读 ${p.goal - p.verified_count} 本不同的书就能领到纪念章！`}</div>
        </div>
      </div>
      <div class="cover-wall">
        ${p.records.map(r => coverCell(r)).join("")}
      </div>`;
  }
  $("#child-content").innerHTML = html;
}

function medalText(m) {
  if (m.state === "waitlisted") return `🏅 已达标，纪念章候补 #${m.wait_pos || "?"}，补货后通知`;
  if (m.state === "reserved") return `🏅 凭证已生成：<b>${esc(m.code)}</b>，请到馆领取`;
  if (m.state === "issued") return `🏅 纪念章已领取，太棒了！`;
  return "🎉 达标！";
}

function coverCell(r) {
  const book = state.activities
    .flatMap(a => a.books).find(b => b.id === r.book_id) || { title: r.title, cover: "" };
  return `
    <div class="cover-cell">
      <span class="badge ${r.status}">${STATUS_CN[r.status]}</span>
      ${book.cover ? `<img src="${book.cover}" alt="">` : `<div class="t">📖</div>`}
      <div class="t">${esc(book.title)}</div>
      ${r.status === "verified" ? `<div class="dup-warn">✓ 已计入达标</div>` : ""}
    </div>`;
}

// ---------------------------------------------------------------- 家长端
async function renderParent() {
  const uid = state.uid.parent;
  const kids = await api.get(`/api/children?parent_id=${uid}`);
  const myEnrs = await api.get(`/api/enrollments?parent_id=${uid}`);
  const notes = await api.get(`/api/notifications?uid=${uid}`);

  // 报名卡片
  $("#parent-enroll").innerHTML = state.activities.map(a => {
    const joined = myEnrs.filter(e => e.activity_id === a.id);
    const joinable = kids.map(k => {
      const age = ageOn(k.birth_date, a.starts_on);
      const inGroup = age >= a.age_min && age <= a.age_max;
      const already = joined.some(e => e.child_id === k.id);
      return { k, age, inGroup, already };
    });
    return `
    <div class="card">
      <h3>${esc(a.name)} <span class="pill green" style="background:var(--green-soft);color:var(--green)">库存 ${a.medal_stock} 枚</span></h3>
      <div class="meta">${a.starts_on} 至 ${a.ends_on} · 借期 ${a.loan_days} 天 · 完成 ${a.goal_books} 本不同的书</div>
      <div style="margin-top:8px">
        ${joinable.map(({ k, age, inGroup, already }) => `
          <span class="mini" style="display:inline-flex;margin:3px 6px 3px 0;
               ${!inGroup ? "opacity:.5" : ""}">
            <span class="mt">${esc(k.name)}（开始日 ${age} 岁）</span>
            ${already ? `<span class="status-tag st-verified">已报名</span>`
              : inGroup
                ? `<button class="btn small" onclick="doEnroll(${a.id},${k.id})">报名</button>`
                : `<span class="hint" title="按活动开始日周岁判定">超出 ${a.age_min}-${a.age_max} 岁组</span>`}
          </span>`).join("")}
      </div>
    </div>`;
  }).join("");

  // 记录 + 待核验 + 补录入口
  let recHtml = "";
  for (const e of myEnrs) {
    const p = await api.get(`/api/progress?enrollment_id=${e.id}`);
    const act = state.activities.find(a => a.id === p.activity.id);
    const kid = kids.find(k => k.id === p.enrollment.child_id);
    const recordedBooks = new Set(p.records.map(r => r.book_id));
    const available = act.books.filter(b => !recordedBooks.has(b.id));
    recHtml += `
    <div class="card" style="margin-bottom:12px">
      <h3>${esc(kid.name)} · ${esc(act.name)}</h3>
      <div class="meta">进度 ${p.verified_count}/${p.goal}
        ${p.medal ? "· " + esc(medalTextSimple(p.medal)) : ""}</div>
      ${p.records.map(r => recRow(r, e.id)).join("") || `<div class="empty">还没有阅读记录</div>`}
      <div class="inline" style="margin-top:8px">
        <select id="new-book-${e.id}">
          ${available.length
            ? available.map(b => `<option value="${b.id}">${esc(b.title)}</option>`).join("")
            : `<option>书目都已记录</option>`}
        </select>
        <input id="new-note-${e.id}" placeholder="一句话心得（可选）">
        <button class="btn small navy" id="new-book-btn-${e.id}" ${available.length ? "" : "disabled"}
          >提交/补录记录</button>
      </div>
      <div class="dup-warn" id="dup-hint-${e.id}"></div>
    </div>`;
  }
  $("#parent-records").innerHTML = recHtml || `<div class="card">先为孩子报名活动。</div>`;

  $("#parent-notes").innerHTML = notes.length
    ? notes.map(n => `<div class="note"><div class="nt">${esc(n.sent_at)} · ${esc(n.kind)}</div>
        <b>${esc(n.title)}</b><br>${esc(n.body)}</div>`).join("")
    : `<div class="empty">暂无提醒</div>`;

  // 绑定新书选择 + 提交
  for (const e of myEnrs) {
    const sel = $(`#new-book-${e.id}`), note = $(`#new-note-${e.id}`);
    const btn = sel.parentElement.querySelector("button");
    if (btn && sel.options.length && !btn.disabled) {
      btn.onclick = () => doUpsert(e.id, +sel.value, true, note.value);
    }
    markDuplicateBooks(e.id);
  }
  // 补录按钮
  $$("[data-edit-record]").forEach(b =>
    b.onclick = () => editRecord(+b.dataset.editRecord));
}

function markDuplicateBooks(enrId) {
  // 同 ISBN 的不同条目提示“与已记录书目为同一本，重复不计入达标”
  const hint = $(`#dup-hint-${enrId}`);
  if (!hint) return;
}

function recRow(r, enrId) {
  const book = state.activities.flatMap(a => a.books).find(b => b.id === r.book_id) || {};
  const dup = state.activities.flatMap(a => a.books)
    .find(b => b.isbn && b.isbn === book.isbn && b.id !== book.id);
  return `
    <div class="rec-row">
      <div class="who">
        <b>${esc(book.title || r.title)}</b>
        ${dup ? `<div class="dup-warn">⚠ 馆内有重复书目条目（同 ISBN），系统按“同一本书”只计一次</div>` : ""}
      </div>
      <span class="loan-tag ${r.loan_synced ? "loan-yes" : "loan-no"}">
        ${r.loan_synced ? "借阅已同步" : "借阅未同步"}</span>
      <span class="status-tag st-${r.status}">${STATUS_CN[r.status]}</span>
      <button class="btn small ghost" data-versions="${r.id}">版本 v${r.current_version}</button>
      ${r.status === "draft" || r.status === "rejected"
        ? `<button class="btn small" data-submit="${r.id}">提交核验</button>` : ""}
      ${r.status !== "verified"
        ? `<button class="btn small ghost" data-edit-record="${r.id}">补录/修改</button>` : ""}
    </div>`;
}

function medalTextSimple(m) {
  if (m.state === "waitlisted") return `纪念章候补 #${m.wait_pos || "?"}`;
  if (m.state === "reserved") return `凭证 ${m.code}`;
  if (m.state === "issued") return "纪念章已领取";
  return "";
}

window.doEnroll = async (actId, childId) => {
  try {
    await api.post("/api/enroll", { activity_id: actId, child_id: childId, uid: state.uid.parent });
    toast("报名成功", "ok");
    renderParent();
  } catch (e) { toast(e.message, "err"); }
};

window.doUpsert = async (enrId, bookId, submit, note = "", readOn = "") => {
  if (!bookId) return;
  try {
    const r = await api.post("/api/records", {
      enrollment_id: enrId, book_id: bookId, uid: state.uid.parent, note, read_on: readOn,
    });
    if (submit) await api.post(`/api/records/${r.record_id}/submit`, { uid: state.uid.parent });
    toast("记录已保存并提交（修改已留版本）", "ok");
    renderParent();
  } catch (e) { toast(e.message, "err"); }
};

async function editRecord(rid) {
  const versions = await api.get(`/api/versions?record_id=${rid}`);
  const last = versions[versions.length - 1];
  openModal(`
    <h3>补录 / 修改记录</h3>
    <p class="meta" style="color:var(--muted)">修改后状态回到草稿，需要重新提交；每次修改都会保留一个版本。</p>
    <textarea id="edit-note" style="width:100%;min-height:80px" class="card">${esc(last.note)}</textarea>
    <div style="margin-top:10px; text-align:right">
      <button class="btn" id="edit-save">保存新版本</button>
    </div>
    ${versionsList(versions)}
  `);
  $("#edit-save").onclick = async () => {
    // 找到该记录的 enrollment/book（通过当前 DOM 上下文不易拿，直接走通用接口需要二者；
    // 用版本接口信息 + 查询接口间接获得）
    const note = $("#edit-note").value;
    await saveEdit(rid, note, last);
  };
}

async function saveEdit(rid, note, lastVersion) {
  // 从家长的所有 progress 中定位 enrollment_id
  const myEnrs = await api.get(`/api/enrollments?parent_id=${state.uid.parent}`);
  for (const e of myEnrs) {
    const p = await api.get(`/api/progress?enrollment_id=${e.id}`);
    const rec = p.records.find(r => r.id === rid);
    if (rec) {
      try {
        await api.post("/api/records", {
          enrollment_id: e.id, book_id: rec.book_id,
          uid: state.uid.parent, note, read_on: rec.read_on,
        });
        $("#modal").classList.add("hidden");
        toast("已保存新版本", "ok");
        renderParent();
      } catch (err) { toast(err.message, "err"); }
      return;
    }
  }
}

// 提交按钮事件委托
document.addEventListener("click", async (ev) => {
  const sub = ev.target.closest("[data-submit]");
  if (sub) {
    try {
      await api.post(`/api/records/${sub.dataset.submit}/submit`, { uid: state.uid.parent });
      toast("已提交馆员核验", "ok");
      renderParent();
    } catch (e) { toast(e.message, "err"); }
  }
  const ver = ev.target.closest("[data-versions]");
  if (ver) {
    const vs = await api.get(`/api/versions?record_id=${ver.dataset.versions}`);
    openModal(`<h3>记录版本历史</h3>${versionsList(vs)}`);
  }
});

function versionsList(vs) {
  return `<div class="versions">${vs.map(v => `
    <div class="v">
      <div class="vh">v${v.version} · ${KIND_CN[v.change_kind] || v.change_kind}
        · ${esc(v.changed_by_name)} · ${esc(v.created_at)}</div>
      ${esc(v.note || "（无备注）")}
    </div>`).join("")}</div>`;
}

function openModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal").classList.remove("hidden");
}

// ---------------------------------------------------------------- 馆员端
async function renderLibrarian() {
  const aid = +$("#lib-activity").value;
  const q = await api.get(`/api/queue?activity_id=${aid}`);
  $("#lib-stock").textContent = `纪念章库存 ${q.activity.medal_stock} 枚`;

  const wl = q.waitlist;
  const wb = $("#lib-waitlist");
  if (wl.length) {
    wb.classList.add("show");
    wb.innerHTML = "🕓 候补队列（按达标时间先后）：" +
      wl.map(w => `#${w.position} ${esc(w.child_name)}（${esc(w.qualified_at)}）`).join(" → ");
  } else wb.classList.remove("show");

  const lib = state.people.librarians.find(l => l.id === state.uid.librarian);
  const own = new Set((lib.own_children || "").split(",").filter(Boolean).map(Number));

  $("#lib-queue").innerHTML = q.queue.map(item => {
    const mine = own.has(item.child.id);
    const submitted = item.records.filter(r => item.submitted_ids.includes(r.id));
    const borrowedOnly = item.borrowed_only;
    const verifiedCount = item.records.filter(r => r.status === "verified"
      && state.activities.find(a => a.id === aid).books.some(b => b.id === r.book_id)).length;
    const goal = q.activity.goal_books;
    return `
    <div class="lib-child">
      <div class="head">
        <h3>${esc(item.child.name)}</h3>
        <span class="pill">${verifiedCount}/${goal} 已核验</span>
        ${mine ? `<span class="pill" style="background:var(--red)">审核回避：这是 ${esc(lib.name)} 的孩子</span>` : ""}
        <span class="grow"></span>
        <button class="btn navy" data-approve="${item.enrollment.id}" ${mine ? "disabled" : ""}>
          达标审核并发章</button>
      </div>
      <div class="cols">
        <div class="col submitted">
          <h4>📝 已提交、待核对（${submitted.length}）</h4>
          ${submitted.length ? submitted.map(r => `
            <div class="mini">
              <div class="mt">
                ${esc(titleOf(r.book_id))}
                <span class="hint">· ${r.loan_synced ? "借阅已同步" : `借阅未同步（预计 ${esc(r.sync_after || "?")} 后到达）`}</span>
              </div>
              <button class="btn small" data-verify="${r.id}" ${mine || !r.loan_synced ? "disabled" : ""}>
                ${r.loan_synced ? "核对通过" : "等借阅"}</button>
              <button class="btn small ghost" data-reject="${r.id}" ${mine ? "disabled" : ""}>驳回</button>
            </div>`).join("") : `<div class="empty">没有已提交的记录</div>`}
        </div>
        <div class="col borrowed">
          <h4>📖 已借阅、尚未提交记录（${borrowedOnly.length}）</h4>
          ${borrowedOnly.length ? borrowedOnly.map(b => `
            <div class="mini"><div class="mt">${esc(b.title)}<div class="hint">孩子借了书但家长还没提交阅读记录</div></div></div>
          `).join("") : `<div class="empty">没有“只借未提交”的书</div>`}
        </div>
      </div>
    </div>`;
  }).join("") || `<div class="card">该活动暂无报名</div>`;

  $$("[data-approve]").forEach(b => b.onclick = () => approve(+b.dataset.approve));
  $$("[data-verify]").forEach(b => b.onclick = () => verify(+b.dataset.verify));
  $$("[data-reject]").forEach(b => b.onclick = () => reject(+b.dataset.reject));
}

function titleOf(bookId) {
  const b = state.activities.flatMap(a => a.books).find(x => x.id === bookId);
  return b ? b.title : `#${bookId}`;
}

async function verify(rid) {
  try {
    await api.post(`/api/records/${rid}/verify`, { uid: state.uid.librarian });
    toast("借阅与完成情况核对通过", "ok");
    renderLibrarian();
  } catch (e) { toast(e.message, "err"); }
}

async function reject(rid) {
  const comment = prompt("驳回原因（会通知家长补充）：", "借阅信息不完整");
  if (comment === null) return;
  try {
    await api.post(`/api/records/${rid}/reject`, { uid: state.uid.librarian, comment });
    toast("已驳回并通知家长", "ok");
    renderLibrarian();
  } catch (e) { toast(e.message, "err"); }
}

async function approve(enrId) {
  try {
    const r = await api.post("/api/approve", { enrollment_id: enrId, uid: state.uid.librarian });
    if (r.state === "reserved") {
      openModal(`<h3>✅ 审核通过，纪念章已预留</h3>
        <p>库存扣减成功，其他馆员无法重复发章。</p>
        <div class="voucher-box"><div>预约领取凭证</div><code>${esc(r.voucher)}</code></div>`);
    } else {
      toast(`库存不足，已按达标时间进入候补 #${r.position}`, "");
    }
    // 库存数字随活动对象刷新
    const acts = await api.get("/api/activities");
    state.activities = acts;
    renderLibrarian();
  } catch (e) { toast(e.message, "err"); }
}

async function restock() {
  const aid = +$("#lib-activity").value;
  const amount = +$("#restock-n").value;
  try {
    const r = await api.post("/api/restock", { activity_id: aid, amount });
    toast(r.promoted.length
      ? `补货完成，${r.promoted.length} 位候补者已按序转正发章`
      : "补货已入库，暂无候补", "ok");
    state.activities = await api.get("/api/activities");
    renderLibrarian();
  } catch (e) { toast(e.message, "err"); }
}

async function pickup() {
  const code = $("#pickup-code").value.trim();
  if (!code) return toast("请输入凭证号");
  try {
    await api.post("/api/pickup", { code });
    toast("凭证已核销，纪念章发放完成", "ok");
    $("#pickup-code").value = "";
  } catch (e) { toast(e.message, "err"); }
}

boot();
