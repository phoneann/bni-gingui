(() => {
  let dialog;

  const getDialog = () => {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "reel-dialog";
    dialog.innerHTML = `<div class="reel-dialog-shell"><button class="reel-close" type="button" aria-label="關閉影片">×</button><div class="reel-dialog-player"></div><a class="reel-dialog-link" target="_blank" rel="noopener">無法播放？前往 Facebook 觀看</a></div>`;
    dialog.addEventListener("close", () => dialog.querySelector(".reel-dialog-player").replaceChildren());
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog || event.target.closest(".reel-close")) dialog.close();
    });
    document.body.append(dialog);
    return dialog;
  };

  const loadReel = (button) => {
    const facebookUrl = button.dataset.facebookUrl;
    if (!facebookUrl) return;
    const playerDialog = getDialog();
    const frame = document.createElement("iframe");
    frame.src = `https://www.facebook.com/plugins/video.php?href=${encodeURIComponent(facebookUrl)}&show_text=false&width=560`;
    frame.title = button.dataset.reelTitle || "Facebook Reel";
    frame.loading = "lazy";
    frame.allow = "autoplay; clipboard-write; encrypted-media; picture-in-picture; web-share";
    frame.allowFullscreen = true;
    playerDialog.querySelector(".reel-dialog-player").replaceChildren(frame);
    playerDialog.querySelector(".reel-dialog-link").href = facebookUrl;
    playerDialog.showModal();
    if (typeof window.gtag === "function") window.gtag("event", "featured_speaker_reel_load", {event_category: "engagement", event_label: facebookUrl});
  };
  document.addEventListener("click", (event) => {
    const button = event.target.closest(".reel-load");
    if (button) loadReel(button);
  });
})();
