// Tableau de bord de l'organisateur : trois indicateurs et la table AG Grid des transferts.
//
// Tout vient de /organizer/api/dashboard, relu toutes les 5 secondes et après chaque décision.
// Le navigateur ne calcule aucun montant : il affiche les centimes envoyés par le serveur.

const DATA_URL = "/organizer/api/dashboard";
const REFRESH_MS = 5000;
const VERDICTS = { VERT: "Vert", ORANGE: "Orange", ROUGE: "Rouge" };

const message = document.querySelector("#dashboard-message");
const onlyPending = document.querySelector("#only-pending");

function euros(cents) {
  return (cents / 100).toLocaleString("fr-FR", { style: "currency", currency: "EUR" });
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge ${kind}`;
  span.textContent = text;
  return span;
}

function button(label, onClick, kind = "") {
  const element = document.createElement("button");
  element.type = "button";
  element.className = kind;
  element.textContent = label;
  element.addEventListener("click", onClick);
  return element;
}

// Valider ou refuser : le serveur applique la machine à états, puis on relit la table.
async function decide(row, action) {
  message.textContent = "";
  const response = await fetch(`/organizer/api/transfers/${row.id}/${action}`, { method: "POST" });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    message.textContent = data.detail ?? "La décision n'a pas pu être enregistrée.";
  }
  await load();
}

function decisionCell(params) {
  const row = params.data;
  const cell = document.createElement("div");
  cell.className = "decision";
  if (row.can_approve) {
    cell.append(button("Valider", () => decide(row, "approve")));
  }
  if (row.can_refuse) {
    const question = `Refuser le transfert du dossard n°${row.bib_number} ? L'autorisation de paiement sera annulée.`;
    cell.append(button("Refuser", () => window.confirm(question) && decide(row, "refuse"), "secondary"));
  }
  if (!row.can_approve && !row.can_refuse) {
    cell.textContent = row.decision;
  }
  return cell;
}

// Un montant : trié et filtré en euros (45), affiché en clair (45,00 €).
function amount(field, headerName) {
  return {
    headerName,
    filter: "agNumberColumnFilter",
    type: "rightAligned",
    width: 115,
    valueGetter: (params) => params.data[field] / 100,
    valueFormatter: (params) => euros(params.data[field]),
  };
}

const gridApi = agGrid.createGrid(document.querySelector("#transfers-grid"), {
  columnDefs: [
    { field: "race", headerName: "Course", width: 200, tooltipField: "race" },
    { field: "bib_number", headerName: "Dossard", filter: "agNumberColumnFilter", width: 100 },
    {
      field: "state_label",
      headerName: "Statut",
      width: 120,
      cellRenderer: (params) => badge(params.value, `state-${params.data.state}`),
    },
    { field: "buyer", headerName: "Acheteur", width: 145 },
    amount("price_cents", "Prix"),
    amount("organizer_fee_cents", "Commission orga"),
    {
      headerName: "Verdict IA",
      width: 110,
      valueGetter: (params) => VERDICTS[params.data.verdict] ?? "",
      cellRenderer: (params) => (params.value ? badge(params.value, `verdict-${params.data.verdict}`) : ""),
    },
    {
      field: "explanation",
      headerName: "Explication de l'IA",
      flex: 1,
      minWidth: 260,
      wrapText: true,
      autoHeight: true,
      cellClass: "explanation",
    },
    {
      headerName: "Décision",
      width: 195,
      // Épinglée à droite : les boutons restent à l'écran même quand la table est plus large que la page.
      pinned: "right",
      sortable: false,
      filter: false,
      // La cellule n'est redessinée que si cette valeur change.
      valueGetter: (params) => `${params.data.can_approve}-${params.data.can_refuse}-${params.data.decision}`,
      cellRenderer: decisionCell,
    },
  ],
  defaultColDef: {
    sortable: true,
    resizable: true,
    filter: "agTextColumnFilter",
    floatingFilter: true,
    wrapHeaderText: true,
    autoHeaderHeight: true,
  },
  rowData: [],
  getRowId: (params) => String(params.data.id),
  domLayout: "autoHeight",
  pagination: true,
  paginationPageSize: 20,
  paginationPageSizeSelector: [20, 50, 100],
  // Un filtre de plus, hors colonnes : la case « Seulement les transferts à décider ».
  isExternalFilterPresent: () => onlyPending.checked,
  doesExternalFilterPass: (node) => node.data.can_approve,
  localeText: {
    noRowsToShow: "Aucun transfert",
    filterOoo: "Filtrer…",
    contains: "Contient",
    notContains: "Ne contient pas",
    equals: "Égal à",
    notEqual: "Différent de",
    startsWith: "Commence par",
    endsWith: "Finit par",
    blank: "Vide",
    notBlank: "Non vide",
    lessThan: "Inférieur à",
    lessThanOrEqual: "Inférieur ou égal à",
    greaterThan: "Supérieur à",
    greaterThanOrEqual: "Supérieur ou égal à",
    inRange: "Entre",
    andCondition: "ET",
    orCondition: "OU",
    page: "Page",
    of: "sur",
    to: "à",
    pageSizeSelectorLabel: "Lignes par page :",
  },
});

onlyPending.addEventListener("change", () => gridApi.onFilterChanged());

function showIndicators(indicators) {
  const plural = (count) => (count > 1 ? "s" : "");
  document.querySelector("#indicator-reassigned").textContent = indicators.reassigned;
  document.querySelector("#indicator-reassigned-label").textContent =
    `dossard${plural(indicators.reassigned)} réattribué${plural(indicators.reassigned)}`;
  const saved = indicators.saved_percent === null ? "–" : `${indicators.saved_percent} %`;
  document.querySelector("#indicator-saved").textContent = saved;
  document.querySelector("#indicator-saved-label").textContent =
    `de places sauvées : ${indicators.reassigned} sur ${indicators.listed} dossard${plural(indicators.listed)} mis en vente`;
  document.querySelector("#indicator-commission").textContent = euros(indicators.organizer_commission_cents);
}

async function load() {
  try {
    const response = await fetch(DATA_URL);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    showIndicators(data.indicators);
    gridApi.setGridOption("rowData", data.transfers);
  } catch (error) {
    message.textContent = "Les transferts n'ont pas pu être chargés.";
    console.error(error);
  }
}

load();
setInterval(load, REFRESH_MS);
