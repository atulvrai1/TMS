const notificationCount = document.getElementById("notification-count");

if (notificationCount) {
    const toast = document.getElementById("notification-toast");
    const toastMessage = document.getElementById("notification-toast-message");
    const dismissToast = document.getElementById("notification-toast-dismiss");
    let currentCount = Number(notificationCount.dataset.count);
    let toastTimeout;

    const updateBadge = (count) => {
        notificationCount.dataset.count = String(count);
        notificationCount.textContent = count > 99 ? "99+" : String(count);
        notificationCount.setAttribute("aria-label", `${count} pending notifications`);
        notificationCount.hidden = count === 0;
    };

    const showToast = (newCount) => {
        toastMessage.textContent = `${newCount} new notification${newCount === 1 ? "" : "s"} need your attention.`;
        toast.hidden = false;
        window.clearTimeout(toastTimeout);
        toastTimeout = window.setTimeout(() => {
            toast.hidden = true;
        }, 8000);
    };

    dismissToast.addEventListener("click", () => {
        window.clearTimeout(toastTimeout);
        toast.hidden = true;
    });

    const refreshNotificationCount = async () => {
        const response = await fetch(notificationCount.dataset.countUrl, {
            headers: { Accept: "application/json" },
            credentials: "same-origin",
        });
        if (!response.ok) {
            throw new Error(`Notification count request failed (${response.status}).`);
        }

        const result = await response.json();
        if (!Number.isInteger(result.count) || result.count < 0) {
            throw new Error("Notification count response was invalid.");
        }

        if (result.count > currentCount) {
            showToast(result.count - currentCount);
        }
        currentCount = result.count;
        updateBadge(currentCount);
    };

    window.setInterval(() => {
        if (document.visibilityState === "visible") {
            refreshNotificationCount().catch((error) => {
                console.error("Unable to refresh admin notifications.", error);
            });
        }
    }, 30000);
}


// Accessible mobile navigation: keep the operations menu out of the way on small screens.
(() => {
    const toggle = document.getElementById("mobile-menu-toggle");
    const navigation = document.getElementById("sidebar-navigation");
    if (!toggle || !navigation) return;

    const closeMenu = () => {
        document.body.classList.remove("menu-open");
        toggle.setAttribute("aria-expanded", "false");
        toggle.innerHTML = "☰ <span>Menu</span>";
    };
    toggle.addEventListener("click", () => {
        const open = !document.body.classList.contains("menu-open");
        document.body.classList.toggle("menu-open", open);
        toggle.setAttribute("aria-expanded", String(open));
        toggle.innerHTML = open ? "✕ <span>Close</span>" : "☰ <span>Menu</span>";
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeMenu();
    });
    navigation.addEventListener("click", (event) => {
        if (event.target.closest("a")) closeMenu();
    });
})();

// Quick, client-side filtering makes long operational and finance tables easier to scan.
// It only filters rows already rendered by the server; it does not change stored data.
(() => {
    const tables = document.querySelectorAll(".fleet-table");
    tables.forEach((table, index) => {
        const body = table.tBodies && table.tBodies[0];
        if (!body) return;
        const rows = Array.from(body.rows);
        if (rows.length < 8) return;

        const container = table.closest(".table-container");
        if (!container || container.querySelector(".table-quick-search")) return;

        const wrapper = document.createElement("div");
        wrapper.className = "table-quick-search";
        const label = document.createElement("label");
        const input = document.createElement("input");
        const inputId = `table-search-${index + 1}`;
        label.htmlFor = inputId;
        label.textContent = "Quick find";
        input.id = inputId;
        input.type = "search";
        input.placeholder = "Search visible rows…";
        input.autocomplete = "off";
        input.setAttribute("aria-describedby", `${inputId}-status`);
        const status = document.createElement("span");
        status.id = `${inputId}-status`;
        status.className = "form-help";
        status.setAttribute("aria-live", "polite");
        status.textContent = `${rows.length} rows`;
        wrapper.append(label, input);
        container.parentNode.insertBefore(wrapper, container);
        wrapper.appendChild(status);

        const empty = document.createElement("div");
        empty.className = "table-search-empty";
        empty.textContent = "No rows match your search. Try another keyword.";
        empty.setAttribute("role", "status");
        container.insertAdjacentElement("afterend", empty);

        input.addEventListener("input", () => {
            const query = input.value.trim().toLocaleLowerCase();
            let visible = 0;
            rows.forEach((row) => {
                const match = row.textContent.toLocaleLowerCase().includes(query);
                row.hidden = !match;
                if (match) visible += 1;
            });
            status.textContent = `${visible} of ${rows.length} rows`;
            empty.style.display = visible ? "none" : "block";
            container.style.display = visible ? "" : "none";
        });
    });
})();

// Prevent accidental double-submission on forms explicitly marked as one-time actions.
document.querySelectorAll("form[data-disable-on-submit]").forEach((form) => {
    form.addEventListener("submit", () => {
        const submit = form.querySelector('button[type="submit"], input[type="submit"]');
        if (!submit) return;
        submit.disabled = true;
        if (submit.tagName === "BUTTON") {
            submit.dataset.originalText = submit.textContent;
            submit.textContent = "Saving…";
        }
    });
});

/* FleetFlow: generate mobile card labels from table headings */
(() => {
    const labelTable = (table) => {
        if (!(table instanceof HTMLTableElement)) return;

        const headers = Array.from(
            table.querySelectorAll('thead th')
        ).map((th) => th.innerText.trim());

        if (!headers.length) return;

        table.querySelectorAll('tbody tr').forEach((row) => {
            const cells = Array.from(row.children).filter(
                (cell) => cell.tagName === 'TD'
            );

            // Leave empty-state and spanning rows unchanged.
            if (cells.some((cell) => cell.colSpan > 1)) return;

            cells.forEach((cell, index) => {
                if (!cell.hasAttribute('data-label')) {
                    cell.setAttribute(
                        'data-label',
                        headers[index] || `Field ${index + 1}`
                    );
                }
            });
        });
    };

    const scanTables = (root = document) => {
        if (root instanceof HTMLTableElement) {
            labelTable(root);
        }

        if (root.querySelectorAll) {
            root.querySelectorAll('table.fleet-table').forEach(labelTable);
        }
    };

    const start = () => {
        scanTables();

        // Also label records added later by JavaScript.
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        scanTables(node);
                    }
                });

                if (
                    mutation.type === 'childList' &&
                    mutation.target instanceof HTMLTableSectionElement
                ) {
                    scanTables(mutation.target.closest('table'));
                }
            });
        });

        observer.observe(document.body, {
            childList: true,
            subtree: true
        });
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
        start();
    }
})();
