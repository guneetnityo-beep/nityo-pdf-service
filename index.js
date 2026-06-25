const express    = require("express");
const puppeteer  = require("puppeteer");
const Handlebars = require("handlebars");
const cors       = require("cors");
const fs         = require("fs");
const path       = require("path");

const app = express();
app.use(cors());
app.use(express.json({ limit: "10mb" }));

function formatDate(d) {
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}
function addDays(d, n) {
  const r = new Date(d); r.setDate(r.getDate() + n); return r;
}
function parseIDR(v) {
  return parseInt(String(v || "0").replace(/\D/g, "")) || 0;
}
function idr(n) {
  const abs = Math.abs(Math.round(n));
  return (n < 0 ? "- " : "") + "IDR " + abs.toLocaleString("id-ID");
}

// Register helpers
Handlebars.registerHelper("eq", (a, b) => a === b);

// Load + compile template at startup — Railway must redeploy if template changes
const templateSrc = fs.readFileSync(path.join(__dirname, "template.html"), "utf8");
const template    = Handlebars.compile(templateSrc);

// Load logo once
let logoBase64 = "";
try {
  const p = path.join(__dirname, "nityo-logo.png");
  if (fs.existsSync(p)) logoBase64 = fs.readFileSync(p).toString("base64");
} catch (_) {}

app.post("/generate-pdf", async (req, res) => {
  try {
    const data = { ...req.body };

    // ── Flatten sender / manager ──────────────────────────
    data.smEmail          = (data.sender  && data.sender.email)           || "";
    data.smName           = (data.manager && data.manager.name)           || "";
    data.smTitle          = (data.manager && data.manager.title)          || "";
    data.smSignatureBase64= (data.manager && data.manager.signatureBase64)|| "";
    data.logoBase64       = logoBase64;
    data.quotationDate    = data.quotationDate || formatDate(new Date());
    data.validUntil       = data.validUntil    || formatDate(addDays(new Date(), 30));

    // ── Enrich line items ────────────────────────────────
    data.lineItems = (data.lineItems || []).map((item, i) => {
      const rate   = parseIDR(item.monthlyRate);
      const months = parseInt(item.durationMonths) || 1;
      const total  = rate * months;
      return {
        ...item,
        no                  : i + 1,
        totalAmount         : total,
        monthlyRateFormatted: idr(rate),
        totalFormatted      : idr(total),
        durationLabel       : `${months} Month${months > 1 ? "s" : ""}`,
      };
    });

    const lineSubtotal = data.lineItems.reduce((s, i) => s + i.totalAmount, 0);

    // ── Breakdown rows ───────────────────────────────────
    // Each breakdownRow: { label, type: "subtotal"|"add"|"subtract"|"percent"|"ppn"|"total", value, formatted }
    const bdRows = [];

    // Line item subtotals (always shown in breakdown)
    data.lineItems.forEach(item => {
      bdRows.push({ label: item.description, type: "item", formatted: item.totalFormatted });
    });

    // Subtotal row
    bdRows.push({ label: "Subtotal", type: "subtotal", formatted: idr(lineSubtotal) });

    // Custom adjustment rows from UI
    let running = lineSubtotal;
    (data.breakdownRows || []).forEach(row => {
      if (!row.label) return;
      let amount = 0;
      if (row.kind === "add") {
        amount = parseIDR(row.value);
        running += amount;
        bdRows.push({ label: row.label, type: "add", formatted: "+ " + idr(amount) });
      } else if (row.kind === "subtract") {
        amount = parseIDR(row.value);
        running -= amount;
        bdRows.push({ label: row.label, type: "subtract", formatted: "- " + idr(amount) });
      } else if (row.kind === "percent") {
        const pct = parseFloat(row.value) || 0;
        amount = Math.round(lineSubtotal * pct / 100);
        running += amount;
        bdRows.push({ label: `${row.label} (${pct}%)`, type: "add", formatted: (amount >= 0 ? "+ " : "- ") + idr(Math.abs(amount)) });
      }
    });

    // PPN
    let grandTotal = running;
    if (data.includePpn) {
      const ppn = Math.round(running * 0.11);
      grandTotal = running + ppn;
      bdRows.push({ label: "PPN 11%", type: "ppn", formatted: "+ " + idr(ppn) });
    }

    bdRows.push({ label: data.includePpn ? "Grand Total (incl. PPN)" : "Grand Total (excl. PPN)", type: "total", formatted: idr(grandTotal) });

    data.bdRows          = bdRows;
    data.showBreakdown   = !!data.showBreakdown;
    data.grandTotalFormatted = idr(grandTotal);

    // ── Render ────────────────────────────────────────────
    const html    = template(data);
    const browser = await puppeteer.launch({
      headless: "new",
      args: ["--no-sandbox", "--disable-setuid-sandbox"],
    });
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: "networkidle0" });
    const pdfBuffer = await page.pdf({
      format         : "A4",
      printBackground: true,
      margin         : { top: "12mm", bottom: "18mm", left: "14mm", right: "14mm" },
    });
    await browser.close();

    res.set({
      "Content-Type"       : "application/pdf",
      "Content-Disposition": `attachment; filename="${data.quotationNumber || "quotation"}.pdf"`,
      "Content-Length"     : pdfBuffer.length,
    });
    res.send(pdfBuffer);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: err.message });
  }
});

app.get("/health", (_, res) => res.json({ status: "ok" }));

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => console.log(`PDF service on :${PORT}`));