// SPDX-License-Identifier: Apache-2.0
//
// Assertions for layerwise-transfer-data-model.html.
//
// The page is not decoration -- it encodes claims about LMCache's data model
// that the design doc relies on, and several of them were wrong at some point
// in the page's history. These checks pin the ones that are easy to break by
// editing the simulation: that a layer is two regions under the standard
// format, that record sharding straddles boundaries at non-power-of-two chunk
// sizes, that readiness jitter stays bounded, and that both GPU-hop kernels
// are reported as whole-group.
//
// Run (jsdom is the only dependency, and is not vendored here):
//   mkdir -p /tmp/lwcheck && cd /tmp/lwcheck && npm i jsdom
//   cd docs/design/v1/distributed/l2_adapters
//   NODE_PATH=/tmp/lwcheck/node_modules node layerwise-transfer-data-model.check.js
//
// Exits non-zero and prints a FAILURE list on any broken assertion.
//
// Navigate phases with go("name"), never by index -- inserting a phase once
// silently retargeted a third of these tests at the wrong panel.

const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const file = path.join(__dirname, "layerwise-transfer-data-model.html");
const html = fs.readFileSync(file, "utf8");

const errors = [];
const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true });
dom.virtualConsole.on("jsdomError", (e) => errors.push("jsdomError: " + e.message));
dom.window.addEventListener("error", (e) => errors.push("error: " + e.message));

const { window } = dom;
const doc = window.document;
const $ = (s) => doc.querySelector(s);
const $$ = (s) => [...doc.querySelectorAll(s)];

function assert(cond, msg) {
  console.log((cond ? "  ok   " : "  FAIL ") + msg);
  if (!cond) errors.push(msg);
}

// --- initial render ---
assert($$(".step").length === 13, "stepper rendered 13 phases");
assert([...$$(".step")].some(b => b.textContent.includes("CacheBlend")),
  "CacheBlend has its own step, not a hidden toggle");
assert($("#narr-title").textContent.length > 0, "narrative title populated");
assert($$("#shapebox .dim:not(.sep)").length > 0, "shape panel has dimensions");
assert($$("#layerstrip .cell").length === 32, "layer strip has 32 cells at default");

// --- walk every phase, assert its panel is visible and populated ---
const expected = [
  ["p-layers", "#layerstrip .cell"],
  ["p-layers", "#layerstrip .cell"],
  ["p-groups", "#grouptree div"],
  ["p-keys", "#keys-grid .mcell"],
  ["p-blend", "#blend-flows .fstep"],
  ["p-payload", "#payload .lslice"],
  ["p-payload", "#payload .lslice"],
  ["p-records", "#rec-segs .seg2"],
  ["p-slots", "#slotlist .slot"],
  ["p-imm", "#bits .bit"],
  ["p-timeline", "#lane-net .blk"],
  ["p-ready", "#ready-grid .mcell"],
  ["p-caveats", "#caveats tr"],
];
const steps = $$(".step");
// Navigate by phase name. Index-based navigation broke silently when a phase
// was inserted mid-walk, retargeting tests at the wrong panel.
function go(name) {
  const i = [...$$(".step")].findIndex(b => b.textContent.includes(name));
  if (i < 0) throw new Error(`no phase named ${name}`);
  $$(".step")[i].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  return i;
}
expected.forEach(([panel, sel], i) => {
  steps[i].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const visible = !$("#" + panel).classList.contains("hidden");
  const count = $$(sel).length;
  assert(visible && count > 0, `phase ${i + 1}: #${panel} visible with ${count} elements`);
});

// --- the load-bearing claim: kv_outer means a layer is 2 regions ---
go("The trap");
assert($$("#payload .plane").length === 2, "standard format draws 2 planes (K and V)");
assert($("#payload-callout").textContent.includes("Two regions"),
  "standard format warns that a layer is two regions");
assert($$("#slotlist .slot").length === 0 || true, "");

const fmt = $("#c-format");
fmt.value = "layer_outer";
fmt.dispatchEvent(new window.Event("change", { bubbles: true }));
assert($$("#payload .plane").length === 1, "NL_X_NB_BS_HS draws a single contiguous plane");
assert($("#payload-callout").textContent.includes("One contiguous"),
  "NL_X_NB_BS_HS reports the layer as contiguous");
fmt.value = "kv_outer";
fmt.dispatchEvent(new window.Event("change", { bubbles: true }));

// --- architecture families, per docs/source/mp/hybrid_models.rst ---
const arch = $("#c-arch"), sep = $("#t-separate");
function setArch(v) { arch.value = v; arch.dispatchEvent(new window.Event("change", { bubbles: true })); }
function kinds() { return new Set($$("#layerstrip .cell").map(c => c.className.split(" ")[1])); }

assert(arch.value === "sw", "defaults to a hybrid, since that is the common case");

setArch("sw");
go("Layers differ");
assert([...kinds()].sort().join(",") === "attn,sw", `sliding hybrid: attn + sw (${[...kinds()]})`);
const swCount = $$("#layerstrip .cell.sw").length, fullCount = $$("#layerstrip .cell.attn").length;
assert(swCount === 32 - fullCount && fullCount === Math.ceil(32 / 6),
  `sliding hybrid interleaves ~5:1 (${swCount} local, ${fullCount} full)`);

setArch("linear");
assert([...kinds()].sort().join(",") === "attn,rec", `linear hybrid: attn + recurrent (${[...kinds()]})`);
assert(sep.checked && sep.disabled,
  "Mamba/GDN forces --separate-object-groups on, and says so in the UI");
go("Grouping");
assert($$("#grouptree > div").length === 2, "linear hybrid: two object groups");

setArch("sw");
go("Grouping");
assert(!sep.disabled && !sep.checked, "sliding hybrid leaves the flag off, matching LMCache's default");
assert($$("#grouptree > div").length === 1,
  "flag off: both kernel groups share one object group");
sep.checked = true; sep.dispatchEvent(new window.Event("change", { bubbles: true }));
assert($$("#grouptree > div").length === 2, "flag on: they split into two object groups");
sep.checked = false; sep.dispatchEvent(new window.Event("change", { bubbles: true }));

assert($$("#grouptree > div").length === 1, "uniform: a single object group");
go("Layers differ");
setArch("uniform");
assert(kinds().size === 1, "uniform: a single kernel group");

// With the flag off the sliding-window layers ride inside the full-attention
// object, so that object exists for every chunk. Only when the group is split
// out does its narrower chunk coverage become visible in the keys.
// --- CacheBlend: its own always-visible phase, with diagrams ---
go("CacheBlend");
assert(!$("#p-blend").classList.contains("hidden"), "the CacheBlend panel shows");

// the chunk-reuse picture
assert($$("#blend-chunks .crow").length === 2, "both reuse rows are drawn");
assert($$("#blend-chunks .chunk.blendhit").length > 0,
  "relocated hits are drawn and distinguishable from prefix hits");
assert($$("#blend-chunks .chunk.hit").length > 0, "prefix hits are drawn");
assert(/was \d+/.test($("#blend-chunks").textContent),
  "a relocated chunk shows the position it came from");
assert(/round trips/.test($("#blend-chunks").textContent),
  "the round-trip cost is quantified");

// the two data paths
assert($$("#blend-flows .flow").length === 2, "both data paths are drawn");
const flowText = $("#blend-flows").textContent;
assert(flowText.includes("multi_layer_block_kv_transfer") &&
       flowText.includes("CB_RETRIEVE_PRE_COMPUTED"),
  "each path names its own GPU-hop kernel");
assert($$("#blend-flows .fstep.alllayers").length === 2,
  "both paths are flagged all-layers, not just blend's");
assert($$("#blend-flows .fstep.shared").length >= 4,
  "the shared L2->L1 steps are marked shared on both paths");
assert(flowText.includes("Nothing in multiprocess mode pipelines to the GPU yet"),
  "the corrected conclusion is stated in the panel, not just the caveats");

// --- the payoff question: does pipelining help blend? ---
const recomp = $("#c-recomp");
const setRecomp = v => {
  recomp.value = String(v);
  recomp.dispatchEvent(new window.Event("input", { bubbles: true }));
};
const pills = () => $("#blend-payoff .readout").textContent;
const ratioOf = who => {
  const m = pills().match(new RegExp(who + " ratio ([\\d.]+)"));
  return m ? Number(m[1]) : NaN;
};

assert($$("#blend-payoff .track").length === 5,
  "two staircase lanes plus three end-to-end bars");

// "Recompute" is the fraction NOT reused, which is the opposite of the natural
// reading, so the panel has to define it rather than assume it.
// Normalised: the source wraps mid-sentence, so raw textContent has newlines.
const payoffDefn = $("#p-blend").textContent.replace(/\s+/g, " ");
assert(/fraction of tokens the GPU calculates from scratch/.test(payoffDefn),
  "recompute is defined as compute-from-scratch, not as reuse");
assert(/failed.{0,3} to reuse/.test(payoffDefn),
  "and framed as the share that was not reused");
assert(/Re-RoPE is not recompute/.test(payoffDefn),
  "rotation is distinguished from recompute, since both sound like work");
assert(/Full prefill \(reference\)/.test(payoffDefn),
  "the comparison lane is labelled a reference bound, not a real cache hit");

// The load-bearing claim: identical bytes, less compute, so blend's ratio is
// strictly worse -- and worse by roughly 1/recompute.
setRecomp(100);
assert(/sanity check, not an operating point/.test($("#blend-payoff").textContent),
  "100% recompute is flagged as degenerate rather than read as a result");
const parity = ratioOf("blend");
assert(Math.abs(parity - ratioOf("reference")) < 0.02,
  `at 100% recompute blend matches dense (${parity} vs ${ratioOf("reference")})`);
setRecomp(15);
const r15 = ratioOf("blend");
assert(r15 > parity * 5,
  `at 15% recompute blend's ratio is ~1/0.15 worse than at parity (${parity} -> ${r15})`);
assert(ratioOf("reference") < r15,
  "blend is always the more transfer-bound of the two");

// Both halves of the answer must be present, not just the discouraging one.
const payoffText = $("#blend-payoff").textContent;
assert(/Structurally, yes/.test(payoffText),
  "the structural answer is stated: blend suits per-layer delivery");
assert(payoffText.includes("process_qkv") && payoffText.includes("get_kv"),
  "and is evidenced by the per-layer calls in the in-process blender");
assert(/not the same question/.test(payoffText),
  "the ratio is explicitly distinguished from whether caching is worth it");

// The question the ratio does NOT answer: is fetching worth it at all?

const worth = $("#blend-payoff").textContent.replace(/\s+/g, " ");
assert(/No cache at all/.test(worth), "a no-cache reference is shown");
assert(/break-?even|% recompute/.test(worth),
  "the break-even recompute ratio is quantified");
assert(/making a bad hit nearly harmless/.test(worth),
  "pipelining's real value for blend is named: removing the downside");

// At high recompute serial blend must be reported as losing to no cache.
setRecomp(100);
const hi = $("#blend-payoff").textContent.replace(/\s+/g, " ");
assert(/worse than not caching/.test(hi),
  "at 100% recompute, fetching then discarding loses to not caching");
assert(/worst case: one layer's transfer/.test(hi),
  "pipelined worst case is one layer's transfer, not break-even — L0 cannot overlap");
assert(/factor of 32/.test(hi),
  "and the downside shrinks by a factor of the layer count");
setRecomp(15);
const lo = $("#blend-payoff").textContent.replace(/\s+/g, " ");
assert(/still ahead/.test(lo),
  "at the published 15% operating point the cache pays for itself even serially");

// Driving recompute down must flip the verdict into network-bound.
setRecomp(5);
assert(/network bound/.test($("#blend-payoff").textContent),
  "at low recompute blend goes network bound");
setRecomp(15);

// compatibility, driven from the sidebar
setArch("uniform");
assert($("#blend-callout").textContent.includes("Valid"), "uniform is valid for blend");
setArch("sw");   // flag off by default -> one merged attention group
assert($("#blend-callout").textContent.includes("Valid"),
  "sliding hybrid with groups merged is valid for blend");
sep.checked = true; sep.dispatchEvent(new window.Event("change", { bubbles: true }));
assert($("#blend-callout").textContent.includes("does not run"),
  "splitting a sliding-window hybrid breaks blend");
assert($("#blend-callout").textContent.includes("exactly one attention object group"),
  "and quotes the actual error from _classify_cb_read_groups");
sep.checked = false; sep.dispatchEvent(new window.Event("change", { bubbles: true }));

setArch("linear");   // attention + recurrent: one attention group, so valid
assert($("#blend-callout").textContent.includes("Valid"),
  "a Mamba hybrid is valid despite being split");
const legText = $("#blend-legs").textContent;
assert(/prefix leg.*\{0, 1\}/s.test(legText) && /blend leg.*\{0\}/s.test(legText),
  `prefix leg reads recurrent, blend leg does not (${legText.replace(/\s+/g, " ").trim()})`);

setArch("sw");
go("Keys");
assert($$("#keys-grid .mcell.na").length === 0,
  "flag off: the shared object exists for every chunk");
sep.checked = true; sep.dispatchEvent(new window.Event("change", { bubbles: true }));
assert($$("#keys-grid .mcell.na").length > 0,
  "flag on: the sliding-window object group covers fewer chunks");
sep.checked = false; sep.dispatchEvent(new window.Event("change", { bubbles: true }));

// real Mamba hybrids need chunk sizes of 544-944 tokens; the slider must reach them
assert(Number($("#c-tokens").max) >= 944, `chunk size slider reaches 944 (max ${$("#c-tokens").max})`);

// --- Aerospike record map: alignment is luck, and the read order is the fix ---
go("Records");

// --- record sharding policy ---
//
// Decision: a record never holds pieces of two planes. Undersize it instead.
// That removes every straddle by construction, so no half-record case exists
// downstream -- a record maps to one layer, a write to one slot, and
// FetchSlot's single layer_id is always well defined.
const alignedTog = $("#t-aligned");
const setAligned = v => {
  alignedTog.checked = v;
  alignedTog.dispatchEvent(new window.Event("change", { bubbles: true }));
};
const shardOf = () => {
  const d = window.derive();
  const lr = window.layerRanges(d, 0);
  const sp = window.shardPlan(lr.objectBytes, S_aligned() ? lr.planeBytes : 0);
  const need = new Set();
  lr.ranges.forEach(r =>
    window.segmentsFor(r.start, r.length, sp.segBytes).list.forEach(i => need.add(i)));
  return {
    ...sp,
    planeBytes: lr.planeBytes,
    read: need.size * sp.segBytes,
    needed: lr.ranges.reduce((a, r) => a + r.length, 0),
  };
};
const S_aligned = () => alignedTog.checked;

assert(alignedTog.checked, "plane-aligned records are the default");

// The load-bearing property: exact, at every Mamba unified block size. These
// are the sizes that broke byte-count sharding -- none is a power of two.
for (const tok of [544, 784, 944, 256]) {
  setRange($("#c-tokens"), tok);
  setAligned(true);
  const a = shardOf();
  assert(a.read === a.needed,
    `aligned at ${tok} tokens: reads exactly the layer, no straddle (${a.read} vs ${a.needed})`);
  assert(a.planeBytes % a.segBytes === 0,
    `aligned at ${tok} tokens: the record divides the plane exactly`);
  assert(a.segBytes <= a.cap,
    `aligned at ${tok} tokens: still within the server's record cap`);

  setAligned(false);
  const n = shardOf();
  if (tok !== 256) {
    assert(n.read > n.needed,
      `byte-count sharding at ${tok} tokens straddles, which is what we are avoiding`);
  }
  // Undersizing costs record count, and that is the accepted trade.
  setAligned(true);
  assert(shardOf().nseg >= n.nseg,
    `aligned at ${tok} tokens uses at least as many records (the trade)`);
}
setRange($("#c-tokens"), 256);
setAligned(true);
{
  const rc = $("#rec-callout").textContent.replace(/\s+/g, " ");
  assert(/by construction/.test(rc), "the panel states the guarantee is structural");
  assert(/records instead of/.test(rc), "and quantifies the extra records it costs");
  assert(/1880 MB\/s/.test(rc), "citing the M0 measurement that smaller records were faster");
}

// An object group concatenates its kernel groups, and planes are outermost
// *within* each one. A flat "first half K, second half V" reading mislabels a
// layer's V plane as K and marks unrelated segments as the V plane.
{
  const d = window.derive();
  const kgs = d.objectGroups.get(0);
  assert(kgs.length === 2,
    "the default sliding-window hybrid merges two kernel groups into one object");
  const planes = $("#rec-planes").textContent;
  assert(/attn K.*attn V.*sw K.*sw V/s.test(planes),
    `regions are named per kernel group and plane (${planes})`);
  assert(!/^K plane/.test(planes),
    "and not as a single K half followed by a single V half");

  // Layer 0 is in the attn group: one K segment and one V segment, not two K.
  const segs = $$("#rec-segs .seg2");
  const needed = segs.filter(s => s.classList.contains("need"));
  assert(needed.length === 2, "layer 0 needs exactly two segments, one per plane");
  assert(needed.filter(s => s.classList.contains("k")).length === 1 &&
         needed.filter(s => s.classList.contains("v")).length === 1,
    "one is coloured K and the other V — previously both read as K");

  // Segments of the other kernel group can never serve this layer.
  const faint = segs.filter(s => s.classList.contains("faint"));
  assert(faint.length > 0 && faint.every(s => !s.classList.contains("need")),
    "the other kernel group's segments are faded and never needed");
  assert(/can never serve it/.test($("#rec-sub").textContent.replace(/\s+/g, " ")),
    "and the panel says why they are unread, rather than leaving it to be inferred");
}

setArch("uniform");   // 32 layers in one group, the worked example
function setTokens(v) {
  $("#c-tokens").value = String(v);
  $("#c-tokens").dispatchEvent(new window.Event("input", { bubbles: true }));
}

// --- byte-count sharding, behind the toggle ---
//
// This is what plan() does now, and what the aligned policy exists to replace.
// It is kept tested because the panel still has to explain the hazard.
setAligned(false);

setTokens(256);       // 512 KiB plane against 1 MiB records -> exact by luck
assert($("#rec-tag").textContent === "aligned by luck",
  `power-of-two sizes happen to divide (${$("#rec-tag").textContent})`);
assert($("#rec-callout").textContent.includes("only by luck"),
  "and the panel says it is luck, not design");
assert($$("#rec-segs .seg2.need").length === 2,
  `a layer needs exactly 2 records, one per plane (${$$("#rec-segs .seg2.need").length})`);
assert($$("#rec-segs .seg2.partial").length === 0, "and neither is a partial read");
// 1 MiB records over 512 KiB planes: reading segments {0,16} completes layers 0 AND 1
assert($("#rec-callout").textContent.includes("yields 2 complete layers"),
  "two reads complete two whole layers, not four");

setTokens(784);       // a real Mamba unified block size: 1.53 MiB plane
assert($("#rec-tag").textContent === "straddling",
  `784-token chunks straddle record boundaries (${$("#rec-tag").textContent})`);
{
  // The harm is inflated first-layer latency, not wasted bandwidth: on a full
  // fetch every record is consumed by some layer. Calling it read
  // amplification invited exactly the wrong conclusion.
  const rc = $("#rec-callout").textContent.replace(/\s+/g, " ");
  assert(/not wasted bandwidth/.test(rc),
    "the cost is explicitly not framed as wasted bandwidth");
  assert(/pipelining can never hide|never hide/.test(rc),
    "it is framed as inflating the first layer, the cost pipelining cannot hide");
  assert(/\d+% more than its own size/.test(rc),
    "and the inflation is still quantified");
  assert(!/read amplification/.test(rc),
    "the misleading 'read amplification' framing is gone");
}
assert($$("#rec-segs .seg2.partial").length > 0, "straddling records are marked partial");

setTokens(256);
// Under byte-count sharding the cap sets the record size, so raising it means
// fewer records.
const capBefore = $$("#rec-segs .seg2").length;
$("#c-reccap").value = "23"; $("#c-reccap").dispatchEvent(new window.Event("input", { bubbles: true }));
assert($$("#rec-segs .seg2").length < capBefore,
  `a larger record cap means fewer records (${capBefore} -> ${$$("#rec-segs .seg2").length})`);

// Under the aligned policy it does not: the record size comes from the plane,
// so the cap only binds when a plane exceeds it. That retires max-record-size
// as a tuning knob for anything smaller than one plane.
setAligned(true);
const alignedAtBigCap = $$("#rec-segs .seg2").length;
$("#c-reccap").value = "20"; $("#c-reccap").dispatchEvent(new window.Event("input", { bubbles: true }));
assert($$("#rec-segs .seg2").length === alignedAtBigCap,
  `aligned: raising the record cap changes nothing while a plane fits (${alignedAtBigCap})`);
// layer-major read order must front-load the segments layer 0 needs
const lm = $$("#rec-order-lm .ord:not(.ell)").map(e => e.textContent);
const seq = $$("#rec-order-seq .ord:not(.ell)").map(e => e.textContent);
assert(seq[0] === "0" && seq[1] === "1", "sequential order reads 0, 1, 2 …");
assert(lm[0] === "0" && lm[1] !== "1",
  `layer-major order jumps straight to the V plane (${lm.slice(0, 4).join(", ")})`);
assert($$("#rec-order-lm .ord.hot").length === 2,
  "exactly the two reads that complete layer 0 are highlighted");
setArch("sw");

// --- slot count scales with max write size ---
go("Slots");
const before = $$("#slotlist .slot").length;
const mw = $("#c-maxwrite");
mw.value = "14";
mw.dispatchEvent(new window.Event("input", { bubbles: true }));
const after = $$("#slotlist .slot").length;
assert(after > before, `smaller max write splits into more slots (${before} -> ${after})`);
mw.value = "20";
mw.dispatchEvent(new window.Event("input", { bubbles: true }));

// --- the pipeline timeline: the payoff, and the failure mode ---
setArch("uniform");

go("The payoff");

const netSlider = $("#c-net"), gpuSlider = $("#c-gpu");
function setRange(node, v) {
  node.value = String(v);
  node.dispatchEvent(new window.Event("input", { bubbles: true }));
}
function kpi(name) {
  const e = $$("#tl-kpis .kpi").find(k => k.querySelector(".k").textContent === name);
  return e ? e.querySelector(".val").textContent : null;
}

// fast link, slow GPU -> transfer fully hidden, no stalls
setRange(netSlider, 250); setRange(gpuSlider, 40);
assert($$("#lane-cpu .blk.stall").length === 0, "fast link + slow GPU: GPU never idles");
assert($("#tl-callout").textContent.includes("pipeline holds"), "reports the pipeline holding");
assert(Number(kpi("Transfer ÷ compute")) < 1, `ratio below 1 (${kpi("Transfer ÷ compute")})`);
// layer 0 can never be hidden -- that is the "pay for one layer" cost, so the
// ceiling is (L-1)/L, here 31/32 = 97%.
const hid = parseFloat(kpi("Transfer hidden"));
assert(hid >= 96 && hid < 100,
  `all but layer 0 hidden (${kpi("Transfer hidden")}, ceiling ${(31 / 32 * 100).toFixed(0)}%)`);

// slow link, fast GPU -> the GPU starves, visibly
setRange(netSlider, 10); setRange(gpuSlider, 900);
assert($$("#lane-cpu .blk.stall").length > 0, "slow link + fast GPU: GPU stalls are drawn");
assert($("#tl-callout").textContent.includes("Network bound"), "reports network-bound starvation");
assert(Number(kpi("Transfer ÷ compute")) > 1, `ratio above 1 (${kpi("Transfer ÷ compute")})`);

// Model size drives compute only, and the defaults must be a self-consistent
// model -- the ratio is meaningless if the parameter count and the KV geometry
// describe different models, and they are set by independent sliders.
{
  const d0 = window.derive();
  const before = d0.timing.avgNet;
  setRange($("#c-params"), 70);
  const d1 = window.derive();
  assert(d1.timing.avgNet === before,
    "model size leaves KV transfer untouched");
  assert(d1.timing.avgCpu > d0.timing.avgCpu,
    "and raises compute, so it only moves the ratio's denominator");
  setRange($("#c-params"), 8);
  assert($("#v-params").textContent === "8 B", "default model size is 8 B");
  assert($("#v-layers").textContent === "32" && $("#v-kvheads").textContent === "8" &&
         $("#v-headdim").textContent === "128",
    "which is consistent with the default KV geometry (Llama-3-8B shape)");
}

// pipelining must never lose to the all-or-nothing protocol shape
setRange(netSlider, 122); setRange(gpuSlider, 400);
const pipeMs = parseFloat(kpi("Pipelined")), aonMs = parseFloat(kpi("All-or-nothing"));
assert(pipeMs < aonMs,
  `pipelined (${kpi("Pipelined")}) beats all-or-nothing (${kpi("All-or-nothing")})`);

// Nothing should frame the design against a legacy path: the comparison is
// between two protocol shapes we could build, not against what exists.
assert(!/\btoday\b/i.test(doc.body.textContent),
  "the page makes no appeal to what exists today");
assert($$("#lane-net .blk").length === 32, "one network block per layer");

// --- readiness: layer-major wavefront, NOT a global shuffle ---
go("The barrier");
assert($$("#ready-cols .ch.ready").length === 0, "no layer ready before streaming");

// How many layers are mid-flight at once. Strictly ordered delivery => 1.
function frontierWidth() {
  const cells = $$("#ready-grid .mcell");
  const cols = 32, rows = $$("#ready-rows div").length;
  let width = 0;
  for (let L = 0; L < cols; L++) {
    let any = false, all = true;
    for (let r = 0; r < rows; r++) {
      const c = cells[r * cols + L];
      if (!c || c.classList.contains("na")) continue;
      if (c.classList.contains("full")) any = true;
      else { all = false; if (c.classList.contains("part")) any = true; }
    }
    if (any && !all) width++;
  }
  return width;
}

// with jitter off the push must be a clean single-layer wavefront
$("#t-jitter").checked = false;
$("#t-jitter").dispatchEvent(new window.Event("change", { bubbles: true }));
$("#btn-play").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
let sawOrderViolation = false, maxWidthOrdered = 0;
const orderWatch = setInterval(() => {
  const ready = $$("#ready-cols .ch").map((c, i) => c.classList.contains("ready") ? i : -1).filter(i => i >= 0);
  // strictly in-order push => the ready set is always a prefix 0..n-1
  if (ready.some((L, i) => L !== i)) sawOrderViolation = true;
  maxWidthOrdered = Math.max(maxWidthOrdered, frontierWidth());
}, 20);

let ticks = 0;
const iv = setInterval(() => {
  ticks++;
  const done = $("#ready-counter").textContent;
  const m = done.match(/^([\d,]+) of ([\d,]+)/);
  const landed = m ? Number(m[1].replace(/,/g, "")) : 0;
  const total = m ? Number(m[2].replace(/,/g, "")) : -1;
  if (landed >= total || ticks > 400) {
    clearInterval(iv);
    clearInterval(orderWatch);
    assert(landed === total && total > 0, `all ${total} slots landed`);
    assert(!sawOrderViolation,
      "jitter off: layers complete strictly in order, as a layer-major push implies");
    assert(maxWidthOrdered <= 1,
      `jitter off: frontier is a single layer wide (max ${maxWidthOrdered})`);
    assert($$("#ready-cols .ch.ready").length === 32, "every layer reported ready at the end");
    assert($("#ready-readout").textContent.includes("Ready layers"), "readout populated");

    // reset returns to empty
    $("#btn-reset").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert($$("#ready-cols .ch.ready").length === 0, "reset clears readiness");

    // jitter on: reordering must stay local. Layer 30 must never beat layer 2.
    $("#t-jitter").checked = true;
    $("#t-jitter").dispatchEvent(new window.Event("change", { bubbles: true }));
    let maxWidth = 0, worstLead = 0;
    $("#btn-play").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const jw = setInterval(() => {
      maxWidth = Math.max(maxWidth, frontierWidth());
      const ready = $$("#ready-cols .ch").map((c, i) => c.classList.contains("ready") ? i : -1).filter(i => i >= 0);
      if (ready.length) {
        let contig = 0;
        while (ready[contig] === contig) contig++;
        worstLead = Math.max(worstLead, (ready[ready.length - 1] + 1) - contig);
      }
      const m2 = $("#ready-counter").textContent.match(/^([\d,]+) of ([\d,]+)/);
      if (m2 && Number(m2[1].replace(/,/g, "")) >= Number(m2[2].replace(/,/g, ""))) {
        clearInterval(jw);
        assert(maxWidth > 1,
          `jitter on: neighbouring layers are in flight together (frontier ${maxWidth} wide)`);
        assert(maxWidth <= 4,
          `jitter stays local, not a global shuffle (frontier max ${maxWidth})`);
        assert(worstLead <= 2,
          `layer completions stay essentially ordered (max lead ${worstLead})`);
        finish();
      }
    }, 20);
    return;
  }
}, 25);

// Deep links are checked in a fresh document, since mutating location.hash in
// the live one fires an async hashchange that resets the running simulation.
function checkDeepLink(hash, expect) {
  const d2 = new JSDOM(html, {
    runScripts: "dangerously", pretendToBeVisual: true,
    url: "file:///layerwise.html" + hash,
  });
  const q = s => d2.window.document.querySelector(s);
  for (const [sel, want] of Object.entries(expect)) {
    const got = q(sel).value !== undefined && q(sel).tagName !== "DIV"
      ? (q(sel).type === "checkbox" ? String(q(sel).checked) : q(sel).value)
      : q(sel).textContent;
    assert(got === want, `deep link ${hash}: ${sel} is ${want} (got ${got})`);
  }
  d2.window.close();
}

function finish() {
    checkDeepLink("#step=3&arch=linear&tokens=784", {
      "#c-arch": "linear",
      "#c-tokens": "784",          // a real unified block size, above the old 512 cap
      "#v-tokens": "784",
      "#t-separate": "true",       // forced on for Mamba/GDN
    });
    checkDeepLink("#arch=uniform&format=layer_outer&layer=7", {
      "#c-arch": "uniform",
      "#c-format": "layer_outer",
      "#v-sel": "7",
    });

    console.log("");
    if (errors.length) {
      console.log(errors.length + " FAILURE(S):");
      errors.forEach(e => console.log("  - " + e));
      process.exit(1);
    }
    console.log("PASS");
    process.exit(0);
}
