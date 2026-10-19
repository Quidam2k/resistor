(function () {
  const actions = document.querySelector(".actions");
  if (!actions) return;

  const letterId = actions.dataset.letterId;
  // Mount prefix ("" standalone, "/apps/resistor" under Pantheon's proxy).
  const ROOT = document.body.dataset.root || "";
  const approveBtn = document.getElementById("approve-btn");
  const rejectBtn = document.getElementById("reject-btn");
  const statusEl = document.getElementById("action-status");

  function setStatus(html) { statusEl.innerHTML = html; }
  function disableButtons() {
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
  }

  async function pollJob(jobId) {
    const start = Date.now();
    const maxMs = 5 * 60 * 1000;
    setStatus('<span class="spinner"></span>Sending… this can take a minute for fax.');
    while (Date.now() - start < maxMs) {
      await new Promise((r) => setTimeout(r, 1000));
      let job;
      try {
        const res = await fetch(ROOT + `/api/delivery/${jobId}/status`);
        job = await res.json();
      } catch (e) {
        continue;
      }
      if (job.status === "done") {
        const note = job.dry_run ? " (DRY RUN — nothing actually sent)" : "";
        setStatus("✅ Sent." + note + " See History for details.");
        setTimeout(() => location.reload(), 1500);
        return;
      }
      if (job.status === "failed") {
        const err = job.result && job.result.error ? job.result.error : "unknown error";
        setStatus("❌ Delivery failed: " + err + " — see letter page for full result.");
        setTimeout(() => location.reload(), 2500);
        return;
      }
    }
    setStatus("⚠️ Still running after 5 minutes — refresh later to check.");
  }

  approveBtn.addEventListener("click", async () => {
    if (!confirm("Approve and send this letter now?")) return;
    disableButtons();
    setStatus('<span class="spinner"></span>Approving…');
    const res = await fetch(ROOT + `/api/letter/${letterId}/approve`, { method: "POST" });
    const data = await res.json();
    if (res.status === 409) {
      setStatus("⚠️ " + (data.message || "Already processed."));
      setTimeout(() => location.reload(), 1500);
      return;
    }
    if (data.manual) {
      setStatus(
        "📋 Approved. This one is a web form — open <a href=\"" + data.url +
        "\" target=\"_blank\">the form</a> and paste the letter text (select it above and copy)."
      );
      return;
    }
    if (data.job_id) {
      await pollJob(data.job_id);
      return;
    }
    if (!res.ok && data.message) {
      setStatus("⚠️ " + data.message);
      return;
    }
    setStatus("❌ Unexpected response: " + JSON.stringify(data));
  });

  rejectBtn.addEventListener("click", async () => {
    const note = prompt("Why reject? Claude reads this note and redrafts:");
    if (note === null) return;
    disableButtons();
    const res = await fetch(ROOT + `/api/letter/${letterId}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: note }),
    });
    const data = await res.json();
    if (res.ok) {
      setStatus("🗑️ Rejected. Claude will see your note next session.");
      setTimeout(() => location.reload(), 1200);
    } else {
      setStatus("⚠️ " + (data.message || "Could not reject."));
    }
  });
})();
