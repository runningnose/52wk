const table = document.querySelector("#stock-table");
if (table) {
  const tbody = table.tBodies[0];
  table.querySelectorAll("th").forEach((th, index) => {
    th.addEventListener("click", () => {
      const direction = th.dataset.direction === "asc" ? "desc" : "asc";
      sortColumn(index, direction);
    });
  });
  sortColumn(0, "desc");
}

function sortColumn(index, direction) {
  table.querySelectorAll("th").forEach(header => delete header.dataset.direction);
  table.querySelectorAll("th")[index].dataset.direction = direction;
  const rows = Array.from(table.tBodies[0].rows);
  rows.sort((a, b) => compare(a.cells[index].innerText, b.cells[index].innerText, direction));
  rows.forEach(row => table.tBodies[0].appendChild(row));
}

function compare(left, right, direction) {
  const leftValue = parseValue(left);
  const rightValue = parseValue(right);
  let result;
  if (typeof leftValue === "number" && typeof rightValue === "number") {
    result = leftValue - rightValue;
  } else {
    result = String(leftValue).localeCompare(String(rightValue), undefined, {numeric: true});
  }
  return direction === "asc" ? result : -result;
}

function parseValue(value) {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "-") return "";
  const suffixMatch = trimmed.match(/^([-+]?[\d,.]+)\s*([kKmMbBtT])$/);
  if (suffixMatch) {
    const multiplier = {k: 1_000, m: 1_000_000, b: 1_000_000_000, t: 1_000_000_000_000}[suffixMatch[2].toLowerCase()];
    return Number(suffixMatch[1].replace(/,/g, "")) * multiplier;
  }
  const numeric = Number(trimmed.replace(/[$,%]/g, "").replace(/,/g, ""));
  return Number.isFinite(numeric) ? numeric : trimmed.toLowerCase();
}
