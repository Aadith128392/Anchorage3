"""
SEO routes for Anchorage 2026 (Flask)
======================================
Add this file to your project and register it in app.py with:

    from seo_routes import register_seo_routes
    register_seo_routes(app)

Covers the two pieces every indexable site needs that templates alone can't
provide: /robots.txt and /sitemap.xml. Keep this list of routes in sync with
the real routes in your app (home, events, about, register, login, privacy_policy).
"""

from flask import Response, url_for
from datetime import datetime, date


def register_seo_routes(app):

    # ------------------------------------------------------------------
    # /robots.txt
    # ------------------------------------------------------------------
    @app.route("/robots.txt")
    def robots_txt():
        lines = [
            "User-agent: *",
            "Allow: /",
            # Don't index account/cart/transaction pages — no SEO value, and
            # keeps Google from wasting crawl budget on logged-in-only pages.
            "Disallow: /cart",
            "Disallow: /login",
            "Disallow: /register/success",
            f"Sitemap: {url_for('sitemap_xml', _external=True)}",
        ]
        return Response("\n".join(lines), mimetype="text/plain")

    # ------------------------------------------------------------------
    # /sitemap.xml
    # ------------------------------------------------------------------
    @app.route("/sitemap.xml")
    def sitemap_xml():
        today = date.today().isoformat()

        # endpoint name, priority, changefreq
        pages = [
            ("home", "1.0", "weekly"),
            ("events", "0.9", "weekly"),
            ("about", "0.6", "monthly"),
            ("register", "0.8", "weekly"),
            ("privacy_policy", "0.2", "yearly"),
        ]

        urls = []
        for endpoint, priority, changefreq in pages:
            try:
                loc = url_for(endpoint, _external=True)
            except Exception:
                # Skip any endpoint that doesn't exist yet in this app
                continue
            urls.append(
                f"""  <url>
    <loc>{loc}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>{changefreq}</changefreq>
    <priority>{priority}</priority>
  </url>"""
            )

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls)
            + "\n</urlset>"
        )
        return Response(xml, mimetype="application/xml")
