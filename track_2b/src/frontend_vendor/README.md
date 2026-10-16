# Pinned browser dependencies

- Vue 3.5.13 production global build: https://unpkg.com/vue@3.5.13/dist/vue.global.prod.js (MIT).
- Tailwind CSS 3.4.17 browser runtime: https://cdn.tailwindcss.com/3.4.17 (MIT).

These files keep the temporary HTML clients functional when external CDNs are unavailable. They are served under `/frontend_vendor/`. Optional fonts and Phosphor icons still use the existing remote URLs. A later UI redesign should choose its own production build pipeline.
