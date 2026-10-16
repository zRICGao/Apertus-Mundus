/* Keyboard/focus behavior shared by the GM's existing Vue dialogs. */
window.WorldUI = (() => {
    const dialogs = [];
    const suppressed = new Map();
    const focusable = 'button, a[href], input, select, textarea, [tabindex]';
    const visibleControls = panel => [...panel.querySelectorAll(focusable)].filter(el =>
        el.tabIndex >= 0 && !el.disabled && !el.closest('[inert]') && el.getClientRects().length);
    function restoreBackground() {
        for (const [el, inert] of suppressed) el.inert = inert;
        suppressed.clear();
    }
    function updateBackground() {
        restoreBackground();
        const top = dialogs.at(-1);
        if (!top) return;
        let child = top.el;
        while (child.parentElement && child.id !== 'app') {
            for (const sibling of child.parentElement.children) {
                if (sibling === child || sibling.tagName === 'SCRIPT') continue;
                suppressed.set(sibling, sibling.inert);
                sibling.inert = true;
            }
            child = child.parentElement;
        }
    }
    const dialog = {
        mounted(el, binding) {
            const panel = el.firstElementChild;
            if (!panel) return;
            const entry = {el, panel, previous: document.activeElement};
            panel.setAttribute('role', 'dialog');
            panel.setAttribute('aria-modal', 'true');
            panel.setAttribute('aria-label', binding.value);
            panel.tabIndex = -1;
            entry.keydown = event => {
                if (dialogs.at(-1) !== entry) return;
                if (event.key === 'Escape' && !event.isComposing && !event.defaultPrevented) {
                    event.preventDefault();
                    el.click(); // Existing click.self handlers own close/reset behavior.
                } else if (event.key === 'Tab') {
                    const controls = visibleControls(panel);
                    const first = controls[0] || panel, last = controls.at(-1) || panel;
                    if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
                        event.preventDefault(); last.focus();
                    } else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
                        event.preventDefault(); first.focus();
                    }
                }
            };
            entry.focusin = event => {
                if (dialogs.at(-1) === entry && !panel.contains(event.target)) panel.focus();
            };
            el._worldDialog = entry;
            dialogs.push(entry);
            updateBackground();
            document.addEventListener('keydown', entry.keydown);
            document.addEventListener('focusin', entry.focusin);
            queueMicrotask(() => { if (el.isConnected) panel.focus(); });
        },
        unmounted(el) {
            const entry = el._worldDialog;
            if (!entry) return;
            document.removeEventListener('keydown', entry.keydown);
            document.removeEventListener('focusin', entry.focusin);
            dialogs.splice(dialogs.indexOf(entry), 1);
            updateBackground();
            if (entry.previous?.isConnected && !entry.previous.closest('[inert]')) entry.previous.focus();
            else dialogs.at(-1)?.panel.focus();
        }
    };
    return {dialog};
})();
