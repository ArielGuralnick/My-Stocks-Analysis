/* ──────────────────────────────────────────────────────────────
   SMART PORTFOLIO SENTINEL — landing page behaviour
   ────────────────────────────────────────────────────────────── */

(() => {
  "use strict";

  /* ── 1. Scroll-triggered reveals (IntersectionObserver) ── */
  const reveals = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && reveals.length) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.18, rootMargin: "0px 0px -8% 0px" });

    reveals.forEach((el, i) => {
      // Stagger delay baked in via inline style so CSS stays clean
      el.style.transitionDelay = `${i * 120}ms`;
      io.observe(el);
    });
  } else {
    // Fallback: show immediately
    reveals.forEach((el) => el.classList.add("is-visible"));
  }


  /* ── 2. Screenshot placeholder swap ──
     If the <img id="whatsapp-screenshot"> has a real src, the CSS
     already hides the placeholder. We additionally handle the case
     where the image fails to load (revert to placeholder). */
  const shot = document.getElementById("whatsapp-screenshot");
  if (shot) {
    shot.addEventListener("error", () => {
      shot.removeAttribute("src");
    });
  }


  /* ── 3. Form submission (Formspree) ── */
  const form    = document.getElementById("lead-form");
  const success = document.getElementById("form-success");
  const errEl   = document.getElementById("form-error");

  if (form && success) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      errEl.hidden = true;

      // Native validation first
      if (!form.checkValidity()) {
        form.reportValidity();
        return;
      }

      const submitBtn = form.querySelector(".submit");
      const originalBtnText = submitBtn.innerHTML;
      submitBtn.disabled = true;
      submitBtn.innerHTML = "<span>Sending…</span>";

      try {
        const data = new FormData(form);
        const res = await fetch(form.action, {
          method: "POST",
          body: data,
          headers: { Accept: "application/json" }
        });

        if (res.ok) {
          // Swap form for success state
          form.hidden = true;
          success.hidden = false;
          success.scrollIntoView({ behavior: "smooth", block: "center" });
        } else {
          throw new Error(`HTTP ${res.status}`);
        }
      } catch (err) {
        console.error("Form submission failed:", err);
        errEl.hidden = false;
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalBtnText;
      }
    });
  }


  /* ── 4. Respect prefers-reduced-motion for smooth scroll ── */
  const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (prefersReduced) {
    document.documentElement.style.scrollBehavior = "auto";
  }


  /* ── 5. Live ticker marquee (fetched from Render backend) ── */
  const BACKEND_URL = "https://portfolio-sentinel-dashboard.onrender.com";
  const marquee = document.getElementById("liveMarquee");
  if (marquee) {
    const symbols = (marquee.dataset.symbols || "").split(",").map(s => s.trim()).filter(Boolean);
    const rows = marquee.querySelectorAll("[data-marquee-row]");

    const renderRows = (html) => {
      rows.forEach((row) => { row.innerHTML = html; });
    };

    const formatPrice = (p) => {
      if (p == null || isNaN(p)) return "—";
      return Number(p).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };

    const formatChange = (c) => {
      if (c == null || isNaN(c)) return { txt: "▪ 0.00%", cls: "" };
      const up = c >= 0;
      return {
        txt: (up ? "▲ " : "▼ ") + Math.abs(c).toFixed(2) + "%",
        cls: up ? "up" : "dn",
      };
    };

    const loadQuotes = () => {
      const url = BACKEND_URL + "/api/quotes?symbols=" + encodeURIComponent(symbols.join(","));
      fetch(url, { method: "GET" })
        .then((r) => {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then((data) => {
          if (!data || !data.ok || !Array.isArray(data.quotes)) {
            throw new Error("Bad response");
          }
          const valid = data.quotes.filter((q) => !q.error && q.price != null);
          if (!valid.length) throw new Error("No quotes returned");
          const html = valid.map((q) => {
            const chg = formatChange(q.change_pct);
            return '<span class="mq"><b>' + q.symbol + '</b> ' +
                   formatPrice(q.price) +
                   ' <i class="' + chg.cls + '">' + chg.txt + '</i></span>';
          }).join("");
          renderRows(html);
        })
        .catch((err) => {
          console.warn("Live quotes unavailable:", err.message || err);
          renderRows('<span class="mq"><b>Live quotes offline</b> — backend waking up, retrying…</span>');
        });
    };

    loadQuotes();
    // Refresh every 60 seconds
    setInterval(loadQuotes, 60 * 1000);
  }

})();
