function fmtMoney(amount) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(amount || 0);
}

function showToast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

async function apiFetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request to ${url} failed (${res.status})`);
  }
  if (res.status === 204) return null;
  return res.json();
}

async function connectAccount(onSuccess) {
  const { link_token } = await apiFetch("/api/link/token", { method: "POST" });
  const handler = Plaid.create({
    token: link_token,
    onSuccess: async (public_token) => {
      try {
        const result = await apiFetch("/api/link/exchange", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ public_token }),
        });
        showToast(`Linked ${result.institution_name} (${result.accounts_linked} accounts)`);
        if (onSuccess) onSuccess();
      } catch (err) {
        showToast(err.message);
      }
    },
  });
  handler.open();
}
