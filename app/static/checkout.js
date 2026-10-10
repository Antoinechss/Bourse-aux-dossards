// Bouton PayPal de la page d'achat (JS SDK v6).
//
// Le navigateur ne décide d'aucun montant. Il demande au serveur de créer la commande, laisse
// l'acheteur l'approuver chez PayPal, puis demande au serveur de bloquer les fonds.

const checkout = document.querySelector("#checkout");
const message = document.querySelector("#checkout-message");
const transferUrl = `/runner/transfers/${checkout.dataset.transferId}`;

function show(text) {
  message.textContent = text;
}

async function post(path) {
  const response = await fetch(transferUrl + path, { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail ?? "Une erreur est survenue.");
  }
  return data;
}

async function createOrder() {
  const order = await post("/order");
  return { orderId: order.id };
}

// Appelée par la balise <script> du SDK PayPal, une fois celui-ci chargé.
async function onPayPalWebSdkLoaded() {
  try {
    const sdk = await window.paypal.createInstance({
      clientId: checkout.dataset.clientId,
      components: ["paypal-payments"],
      pageType: "checkout",
    });
    const methods = await sdk.findEligibleMethods({ currencyCode: "EUR" });
    if (!methods.isEligible("paypal")) {
      show("Le paiement par PayPal n'est pas disponible pour cet achat.");
      return;
    }

    const paymentSession = sdk.createPayPalOneTimePaymentSession({
      async onApprove() {
        show("Paiement approuvé, blocage de la somme en cours…");
        try {
          await post("/authorize");
          // Direction la page de suivi, où l'acheteur dépose son justificatif.
          window.location.href = transferUrl;
        } catch (error) {
          show(error.message);
        }
      },
      onCancel() {
        show("Paiement abandonné : rien n'a été bloqué ni débité.");
      },
      onError(error) {
        show("PayPal a rencontré une erreur. Rien n'a été débité : vous pouvez réessayer.");
        console.error(error);
      },
    });

    const button = document.querySelector("#paypal-button");
    button.removeAttribute("hidden");
    show("");
    button.addEventListener("click", async () => {
      try {
        // On passe la promesse sans l'attendre : le navigateur n'ouvre la fenêtre PayPal que si
        // elle suit immédiatement le clic.
        await paymentSession.start({ presentationMode: "auto" }, createOrder());
      } catch (error) {
        show(error.message);
      }
    });
  } catch (error) {
    show("Le bouton PayPal n'a pas pu se charger. Rechargez la page.");
    console.error(error);
  }
}
