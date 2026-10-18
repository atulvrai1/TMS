# AI coding instructions for FleetFlow

## Project context
- This is FleetFlow, a Flask + SQLite truck-management web application.
- The frontend uses server-rendered Jinja templates and a shared stylesheet at `expense-tracker/static/css/style.css`.
- Shared browser behavior lives in `expense-tracker/static/js/main.js`.
- Dashboard navigation is rendered in `expense-tracker/templates/base.html`.

## Responsive-design rules
1. Inspect the existing template, shared CSS, JavaScript, and page-specific styles before editing.
2. Prefer CSS Grid, Flexbox, intrinsic sizing, `minmax(0, 1fr)`, `min-width: 0`, `max-width: 100%`, and content-based sizing over fixed widths.
3. Do not add a new media query until checking whether an existing breakpoint already handles the component. Avoid duplicate rules and competing overrides.
4. Keep one authoritative mobile dashboard-navigation behavior: the menu button controls visibility and keeps `aria-expanded` accurate. Do not make mobile navigation visible unconditionally.
5. Use CSS media queries for layout. Add JavaScript only for interaction or state that CSS cannot handle.
6. Data-dense tables may scroll horizontally inside `.table-container`; the whole page must not scroll horizontally because of a table.
7. Preserve keyboard navigation, visible focus, semantic HTML, accessible names, and reduced-motion support.
8. Do not change business logic, authentication, database schema, or operational workflows for a styling-only task.
9. Keep the existing visual theme and shared design tokens unless the task explicitly requests a redesign.

## Required responsive verification
- Check widths of 320, 360, 390, 420, 560, 768, 769, 1024, 1280, and 1440 CSS pixels where practical.
- Test the landing/auth pages and dashboard pages, including the mobile menu both closed and open.
- Check for horizontal page overflow, clipped controls, overlapping headers, unreadable table actions, and unreachable navigation items.
- Run the existing tests after changes. If browser automation is available, add or run layout regression checks.
- Report which checks actually ran; never claim a browser/device test passed if it was not executed.
