const input = document.getElementById("token");
const status = document.getElementById("status");

chrome.storage.local.get("lensApiToken", (v) => {
  if (v && v.lensApiToken) input.placeholder = "a token is saved (paste a new one to replace it)";
});

document.getElementById("save").addEventListener("click", () => {
  const token = input.value.trim();
  if (!token) return;
  chrome.storage.local.set({ lensApiToken: token }, () => {
    input.value = "";
    status.textContent = "Saved";
    setTimeout(() => (status.textContent = ""), 2500);
  });
});
