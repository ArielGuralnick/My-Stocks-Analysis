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

})();
