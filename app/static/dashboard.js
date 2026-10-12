// Tableau de bord de l'organisateur : trois indicateurs et la table AG Grid des transferts.
//
// Tout vient de /organizer/api/dashboard, relu toutes les 5 secondes et après chaque décision.
// Le navigateur ne calcule aucun montant : il affiche les centimes envoyés par le serveur.

const DATA_URL = "/organizer/api/dashboard";
const REFRESH_MS = 5000;

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

// Accepter ou refuser le justificatif d'un acheteur : le serveur enregistre la décision, puis on relit la table.
// Le paiement n'est pas concerné : le justificatif est indépendant de l'achat.
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
  if (row.can_decide) {
    cell.append(button("Valider", () => decide(row, "approve")));
    cell.append(button("Refuser", () => decide(row, "refuse"), "secondary"));
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
      field: "proof_label",
      headerName: "Justificatif",
      width: 125,
      cellRenderer: (params) => (params.value ? badge(params.value, `proof-${params.data.proof}`) : ""),
    },
    {
      field: "explanation",
      headerName: "Avis de l'IA sur le justificatif",
      flex: 1,
      minWidth: 260,
      wrapText: true,
      autoHeight: true,
      cellClass: "explanation",
    },
    { field: "signal", headerName: "Signal revendeur", width: 220, wrapText: true, autoHeight: true, cellClass: "explanation" },
    {
      headerName: "Justificatif à vérifier",
      width: 195,
      // Épinglée à droite : les boutons restent à l'écran même quand la table est plus large que la page.
      pinned: "right",
      sortable: false,
      filter: false,
      // La cellule n'est redessinée que si cette valeur change.
      valueGetter: (params) => String(params.data.can_decide),
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
  // Un filtre de plus, hors colonnes : la case « Seulement les justificatifs à vérifier ».
  isExternalFilterPresent: () => onlyPending.checked,
  doesExternalFilterPass: (node) => node.data.can_decide,
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
