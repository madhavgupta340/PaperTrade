// Applies saved display preferences before first paint (kept tiny; loaded synchronously).
(function () {
  try {
    var prefs = JSON.parse(localStorage.getItem('pt-prefs') || '{}');
    var root = document.documentElement;
    if (prefs.theme === 'light' || prefs.theme === 'dark') root.dataset.theme = prefs.theme;
    if (prefs.motion === 'reduced') root.dataset.motion = 'reduced';
  } catch (e) { /* storage unavailable: use system defaults */ }
}());
