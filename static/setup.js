function toggle(source) {
    for (const checkbox of document.getElementsByName("sync_options")) {
        checkbox.checked = source.checked;
        checkbox.dispatchEvent(new Event("change"));
    }
}
if (typeof zip === "undefined") {
    alert("Failed to load ZIP library. Please refresh the page and try again.");
} else {
    zip.configure({ useWebWorkers: false });
}
