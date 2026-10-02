(() => {
  const loadReel = (button) => {
    const target = document.getElementById(button.dataset.reelTarget);
    const facebookUrl = button.dataset.facebookUrl;
    if (!target || !facebookUrl || target.dataset.loaded === "true") return;
    const frame = document.createElement("iframe");
    frame.src = `https://www.facebook.com/plugins/video.php?href=${encodeURIComponent(facebookUrl)}&show_text=false&width=560`;
    frame.title = button.dataset.reelTitle || "Facebook Reel";
    frame.loading = "lazy";
    frame.allow = "autoplay; clipboard-write; encrypted-media; picture-in-picture; web-share";
    frame.allowFullscreen = true;
    target.replaceChildren(frame);
    target.dataset.loaded = "true";
    target.classList.add("is-loaded");
    if (typeof window.gtag === "function") window.gtag("event", "featured_speaker_reel_load", {event_category: "engagement", event_label: facebookUrl});
  };
  document.addEventListener("click", (event) => {
    const button = event.target.closest(".reel-load");
    if (button) loadReel(button);
  });
})();
