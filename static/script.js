# Write starter static/script.js file for modal, usage bar, and error handlers
script_js = """
document.addEventListener("DOMContentLoaded", () => {
  const loginBtn = document.getElementById("loginBtn");
  const signupBtn = document.getElementById("signupBtn");

  if (loginBtn) loginBtn.onclick = () => openModal("loginModal");
  if (signupBtn) signupBtn.onclick = () => openModal("signupModal");
});

function openModal(id) {
  document.getElementById(id).style.display = "flex";
}

function closeModal(id) {
  document.getElementById(id).style.display = "none";
}

function showTerms() {
  openModal("termsModal");
}

// Video usage bar animation
function updateUsageBar(current, total) {
  const usage = document.getElementById("usageBar");
  if (!usage) return;

  const percentage = Math.min((current / total) * 100, 100);
  usage.style.width = percentage + "%";

  if (percentage < 30) usage.style.backgroundColor = "#28a745";
  else if (percentage < 70) usage.style.backgroundColor = "#ffc107";
  else usage.style.backgroundColor = "#dc3545";
}
"""

with open(os.path.join(static_dir, "script.js"), "w") as f:
    f.write(script_js)

"✅ script.js created with modal and usage bar logic."
