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
