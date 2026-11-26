/* RDS Guard — apply the chosen colour theme before the page paints.
   Dark is the default; "Ljust tema" in the footer stores "light". */
(function () {
    var theme = null;
    try { theme = localStorage.getItem('rdsguard-theme'); } catch (e) { /* private mode */ }
    if (theme === 'light') {
        document.documentElement.dataset.theme = 'light';
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.content = '#FFFFFF';
    }
}());
