/**
 * Google Analytics (GA4) loader.
 *
 * Reads the measurement ID from <meta name="ga-measurement-id"> if present.
 * To disable analytics (e.g. in dev), omit the meta tag or leave it as the
 * placeholder value.
 */
(function () {
  var meta = document.querySelector('meta[name="ga-measurement-id"]');
  var id = meta ? meta.getAttribute("content") : "";

  if (!id || id === "G-XXXXXXXXXX") return;

  var script = document.createElement("script");
  script.async = true;
  script.src = "https://www.googletagmanager.com/gtag/js?id=" + id;
  document.head.appendChild(script);

  window.dataLayer = window.dataLayer || [];
  function gtag() {
    window.dataLayer.push(arguments);
  }
  window.gtag = gtag;
  gtag("js", new Date());
  gtag("config", id);
})();
