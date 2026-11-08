(async function () {
const traktFileStatus = document.querySelector(".trakt-file-status");
const TRAKT_DB_NAME = "trakt-migration";
const TRAKT_DB_VERSION = 1;
const TRAKT_STORE_NAME = "client-state";
const TRAKT_DATA_KEY = "trakt_data";

function openDatabase(name, version, storeName) {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(name, version);
        request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(storeName)) {
                db.createObjectStore(storeName);
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
        request.onerror = () => reject(request.error);
    });
}

async function setDatabase(data, name, version, storeName, key) {
    const db = await openDatabase(name, version, storeName);
    return new Promise((resolve, reject) => {
        const transaction = db.transaction(storeName, "readwrite");
        const store = transaction.objectStore(storeName);
        store.put(data, key);
        // A successful request is not yet a committed transaction. Reloading
        // before oncomplete can discard the export in some browsers.
        transaction.oncomplete = () => { db.close(); resolve(); };
        transaction.onerror = () => { db.close(); reject(transaction.error); };
        transaction.onabort = () => { db.close(); reject(transaction.error || new Error("Storage transaction aborted")); };
    });
}

async function getDatabase(name, version, storeName, key) {
    const db = await openDatabase(name, version, storeName);
    return new Promise((resolve, reject) => {
        const transaction = db.transaction(storeName, "readonly");
        const store = transaction.objectStore(storeName);
        const request = store.get(key);
        request.onsuccess = () => resolve(request.result || null);
        request.onerror = () => reject(request.error);
        transaction.oncomplete = () => db.close();
        transaction.onerror = () => db.close();
        transaction.onabort = () => db.close();
    });
}

async function clearDatabase(name, version, storeName, key) {
    const db = await openDatabase(name, version, storeName);
    return new Promise((resolve, reject) => {
        const transaction = db.transaction(storeName, "readwrite");
        const store = transaction.objectStore(storeName);
        store.delete(key);
        transaction.oncomplete = () => { db.close(); resolve(); };
        transaction.onerror = () => { db.close(); reject(transaction.error); };
        transaction.onabort = () => { db.close(); reject(transaction.error || new Error("Storage transaction aborted")); };
    });
}

const LOCAL_DATA_TTL_MS = 24 * 60 * 60 * 1000;
const PRIVATE_LOCAL_KEYS = ["trakt_data", "trakt_data_file", "migration_logs", "migration_retry_options"];

async function clearPrivateLocalData() {
    await clearDatabase(TRAKT_DB_NAME, TRAKT_DB_VERSION, TRAKT_STORE_NAME, TRAKT_DATA_KEY);
    localStorage.removeItem("export_expires_at");
    localStorage.removeItem("logs_expires_at");
    PRIVATE_LOCAL_KEYS.forEach(key => localStorage.removeItem(key));
    localStorage.removeItem("private_data_expires_at");
    // Keep events_url: forgetting a running job could cause duplicate migration writes.
}

function setPrivateDataExpiry(key) {
    localStorage.setItem(key, String(Date.now() + LOCAL_DATA_TTL_MS));
}

async function expirePrivateLocalData() {
    const legacy = localStorage.getItem("private_data_expires_at");
    let cleared = false;
    for (const [key, marker] of [["export_expires_at", "trakt_data_file"], ["logs_expires_at", "migration_logs"]]) {
        let deadline = localStorage.getItem(key);
        if (deadline === null) {
            if (localStorage.getItem(marker) === null && legacy === null) continue;
            deadline = legacy ?? String(Date.now() + LOCAL_DATA_TTL_MS);
            localStorage.setItem(key, deadline);
        }
        if (Number.isFinite(Number(deadline)) && Number(deadline) > Date.now()) continue;
        if (key === "export_expires_at") {
            await clearDatabase(TRAKT_DB_NAME, TRAKT_DB_VERSION, TRAKT_STORE_NAME, TRAKT_DATA_KEY);
            for (const field of ["trakt_data", "trakt_data_file", "migration_retry_options"]) localStorage.removeItem(field);
        } else {
            localStorage.removeItem("migration_logs");
        }
        localStorage.removeItem(key);
        cleared = true;
    }
    localStorage.removeItem("private_data_expires_at");
    return cleared;
}

// Finish startup cleanup before displaying or submitting persisted data.
try { await expirePrivateLocalData(); }
catch { alert("Local data cleanup failed. Check browser storage permissions."); return; }
setInterval(async () => {
    try { if (await expirePrivateLocalData()) location.reload(); } catch {}
}, 60000);
document.getElementById("clear-local-data").addEventListener("click", async () => {
    if (!confirm("Delete the export and logs stored in this browser? A running migration will continue; its reconnect link will be kept.")) return;
    try { await clearPrivateLocalData(); location.reload(); }
    catch { alert("Could not clear local data. Check browser storage permissions."); }
});

async function traktDataHandler() {
    let collectedData;
    try {
        collectedData = await getDatabase(TRAKT_DB_NAME, TRAKT_DB_VERSION, TRAKT_STORE_NAME, TRAKT_DATA_KEY);
    } catch (error) {
        console.error("Error parsing collected Trakt data:", error);
        localStorage.removeItem("trakt_data"); // Clear the invalid data from local storage
        localStorage.removeItem("trakt_data_file"); // Clear the associated file name as well
        clearDatabase(TRAKT_DB_NAME, TRAKT_DB_VERSION, TRAKT_STORE_NAME, TRAKT_DATA_KEY).catch((cleanupError) => {
            console.error("Failed to clear stored Trakt data:", cleanupError);
        });
        alert("Failed to parse the collected Trakt data. Please re-upload your Trakt data file and try again.");
        location.reload(); // Reload the page to reset the form and status indicators
        return;
    }
    if (!collectedData) {
        alert("No Trakt data is available. Please upload your Trakt data file again.");
        localStorage.removeItem("trakt_data_file"); // Clear the associated file name as well
        location.reload(); // Reload the page to reset the form and status indicators
        return;
    }

    return collectedData; // Return the collected data for further processing
}

async function displayTraktLists(listsContainer) {
    let collectedData = await traktDataHandler();
    if (!collectedData) {
        return false; // Stop the function if no valid Trakt data is available
    }

    const sync_lists_choice = document.getElementById("sync_lists_choice");

    const lists = collectedData['lists-lists'] || [];
    for(const list of lists) {
        const listId = list['ids']['trakt'];
        const listName = list['name'];
        const listPrivacy = list['privacy'] == "private" ? "Private" : "Public"; // Convert privacy to title case for display
        const listItemCount = list['item_count'];
        const listCheckbox = document.createElement("input");
        const listForm = document.createElement("form");
        listForm.appendChild(listCheckbox);
        listsContainer.appendChild(listForm);
        listCheckbox.type = "checkbox";
        listCheckbox.id = `list-${listId}`;
        listCheckbox.name = "list_sync_options";
        listCheckbox.checked = sync_lists_choice.checked;
        listCheckbox.value = "true";
        const listLabel = document.createElement("label");
        listLabel.htmlFor = `list-${listId}`;
        listLabel.textContent = `"${listName}" (${listPrivacy}, ${listItemCount} Items)`;
        listForm.appendChild(listCheckbox);
        listForm.appendChild(listLabel);
        listForm.appendChild(document.createElement("br"));
    }
    syncChildListCheckboxesWithParent()
}

function exportBasename(filename) {
    return filename.replaceAll("\\", "/").split("/").pop();
}

function normaliseExportEntryName(filename) {
    const base = exportBasename(filename).toLowerCase();
    if (!base.endsWith(".json")) return null;
    const stem = base.slice(0, -5);
    return traktWantedFiles.find(name => stem === name || stem.startsWith(name + "-") || stem.startsWith(name + "_") || stem.startsWith(name + ".")) || null;
}

function matchesListExportEntry(filename, listId) {
    const base = exportBasename(filename).toLowerCase();
    if (!base.endsWith(".json")) return false;
    const stem = base.slice(0, -5);
    const prefix = `lists-list-${listId}`;
    return stem === prefix || stem.startsWith(prefix + "-") || stem.startsWith(prefix + "_") || stem.startsWith(prefix + ".");
}

function migrationPayload(options) {
    const source = options.trakt_data;
    const data = {};
    function media(row) {
        const key = row.type === "movie" ? "movie" : "show";
        const ids = row[key]?.ids || {};
        return {type: row.type, [key]: {ids: {tmdb: ids.tmdb, trakt: ids.trakt}}};
    }
    const listItem = row => ({...media(row), listed_at: row.listed_at});
    const episode = row => row.type === "episode" ? {episode: {season: row.episode?.season, number: row.episode?.number}} : {};
    function copy(enabled, key, project) {
        if (enabled && Array.isArray(source[key])) data[key] = source[key].map(project);
    }
    copy(options.sync_watchlist_choice, "lists-watchlist", listItem);
    copy(options.sync_lists_choice, "lists-lists", list => ({
        name: list.name, description: list.description, privacy: list.privacy,
        items: list.items?.map(listItem)
    }));
    copy(options.sync_show_watch_history_choice || options.sync_movie_watch_history_choice,
        "watched-history", row => ({...media(row), ...episode(row), watched_at: row.watched_at}));
    copy(options.sync_show_resume_points_choice || options.sync_movie_resume_points_choice,
        "watched-playback", row => ({...media(row), ...episode(row), progress: row.progress, paused_at: row.paused_at}));
    copy(options.sync_dropped_shows_choice, "hidden-dropped", media);
    return {...options, trakt_data: data};
}

function filterSyncOptions(syncOptionsJson) {
    const data = syncOptionsJson.trakt_data;
    const listForm = document.querySelectorAll("input[name='list_sync_options']");
    // Missing selected datasets must remain missing so the server can report them accurately.
    if (!syncOptionsJson.sync_dropped_shows_choice) data['hidden-dropped'] = [];
    if (!syncOptionsJson.sync_lists_choice && Array.isArray(data['lists-lists'])) data['lists-lists'] = [];
    if (Array.isArray(data['watched-playback'])) {
        if (!syncOptionsJson.sync_movie_resume_points_choice) data['watched-playback'] = data['watched-playback'].filter(item => item.type !== "movie");
        if (!syncOptionsJson.sync_show_resume_points_choice) data['watched-playback'] = data['watched-playback'].filter(item => item.type !== "episode");
    }
    if (Array.isArray(data['watched-history'])) {
        if (!syncOptionsJson.sync_movie_watch_history_choice) data['watched-history'] = data['watched-history'].filter(item => item.type !== "movie");
        if (!syncOptionsJson.sync_show_watch_history_choice) data['watched-history'] = data['watched-history'].filter(item => item.type !== "episode");
    }
    if (!syncOptionsJson.sync_watchlist_choice) data['lists-watchlist'] = [];
    if (listForm.length && Array.isArray(data['lists-lists'])) {
        const selectedLists = Array.from(listForm).filter(checkbox => checkbox.checked).map(checkbox => checkbox.id.replace("list-", ""));
        data['lists-lists'] = data['lists-lists'].filter(list => selectedLists.includes(String(list.ids.trakt)));
    }
    return syncOptionsJson;
}

// Add click behavior to the Trakt file status text to trigger file input when clicked
const traktFileStatusText = document.querySelector(".trakt-file-status + .status-text");
const traktWantedFiles = [
    "watched-history",
    "watched-shows",
    "lists-watchlist",
    "lists-lists",
    "watched-playback",
    "user-profile",
    "hidden-dropped"
]; // List of expected files in the Trakt export ZIP

// Add click behavior to the Lists button to toggle the visibility of the lists container
const chooseListsButton = document.getElementById("choose-lists");
const listsContainer = document.querySelector(".lists-container");
const syncListsChoice = document.getElementById("sync_lists_choice");

function syncChildListCheckboxesWithParent() {
    const listCheckboxes = document.querySelectorAll("input[name='list_sync_options']");
    listCheckboxes.forEach((checkbox) => {
        checkbox.checked = syncListsChoice.checked;
        if (syncListsChoice.checked) {
            checkbox.disabled = false; // Enable child checkboxes when parent is checked
        } else {
            checkbox.disabled = true; // Disable child checkboxes when parent is unchecked
        }
    });
}

if (syncListsChoice) {
    syncListsChoice.addEventListener("change", syncChildListCheckboxesWithParent);
}

chooseListsButton.addEventListener("click", () => {
    if (listsContainer.style.display === "none" || listsContainer.style.display === "") {
        listsContainer.style.display = "block";
        chooseListsButton.textContent = "▼"; // Change button text to indicate collapse
        chooseListsButton.title = "Collapse Lists";
        if (listsContainer.childElementCount === 0) {
            displayTraktLists(listsContainer); // Call the function to display the lists
        }
    } else {
        listsContainer.style.display = "none";
        chooseListsButton.textContent = "▲"; // Change button text to indicate expand
        chooseListsButton.title = "Expand Lists";
    }
});

function failTraktFileImport(message, error) {
    alert(message);
    traktFileStatus.classList.remove("status-orange");
    traktFileStatus.classList.remove("status-green");
    traktFileStatus.classList.add("status-red");
    traktFileStatusText.textContent = `Trakt Data File (Invalid File)`;
    localStorage.removeItem("trakt_data_file");
    localStorage.removeItem("trakt_data");
    clearDatabase(TRAKT_DB_NAME, TRAKT_DB_VERSION, TRAKT_STORE_NAME, TRAKT_DATA_KEY).catch((cleanupError) => {
        console.error("Failed to clear stored Trakt data:", cleanupError);
    });
    console.error(message, error);
}

if (traktFileStatusText) {
    traktFileStatusText.addEventListener("click", () => {
        const fileInput = document.createElement("input");
        fileInput.type = "file";
        fileInput.accept = ".zip"; // Accept ZIP files
        fileInput.onchange = async (event) => {
            const file = event.target.files[0];
            if (file) {

                traktFileStatus.classList.remove("status-red");
                traktFileStatus.classList.add("status-orange");
                traktFileStatusText.textContent = `Processing Trakt Data File (${file.name})`;

                let zipReader;
                try {
                    zipReader = new zip.ZipReader(new zip.BlobReader(file));
                    const entries = await zipReader.getEntries();

                    if (entries.length) {

                        localStorage.setItem("trakt_data_file", file.name); // Store the file name in local storage for display
                        traktFileStatus.classList.remove("status-orange");
                        traktFileStatus.classList.add("status-green");
                        traktFileStatusText.textContent = `Trakt Data File (${file.name})`;
                        var collectedData = {};

                        // Parse each entry in the ZIP file and store its content in local storage
                        for (const entry of entries) {
                            if (!entry.directory) {
                                const normalName = normaliseExportEntryName(entry.filename);
                                if (!normalName) continue;

                                const entryBlob = await entry.getData(new zip.BlobWriter());
                                const entryText = await entryBlob.text();

                                // Convert the entry text to JSON to validate it and catch any parsing errors early
                                try {
                                    const jsonFile = JSON.parse(entryText);
                                } catch (error) {
                                    failTraktFileImport(`Failed to parse ${entry.filename} as JSON. Please ensure the file is a valid Trakt data export.`);
                                    return; // Stop processing if any file is invalid
                                }

                                const jsonData = JSON.parse(entryText);
                                if (normalName !== "user-profile" && !Array.isArray(jsonData)) {
                                    failTraktFileImport(`Invalid dataset format in ${entry.filename}: expected an array.`);
                                    return;
                                }
                                if (normalName === "user-profile" && (!jsonData || typeof jsonData !== "object" || Array.isArray(jsonData))) {
                                    failTraktFileImport("Invalid user-profile JSON file.");
                                    return;
                                }
                                if (normalName === "user-profile") {
                                    // Remove sensitive information from the user-profile data, so it never gets sent to the server.
                                    jsonData["joined_at"] = "";
                                    jsonData["location"] = "";
                                    jsonData["about"] = "";
                                    jsonData["gender"] = "";
                                    jsonData["age"] = null;
                                    jsonData["images"] = {};
                                }
                                if (normalName === "lists-lists") {
                                    // Remove sensitive information from the list data, so it never gets sent to the server.
                                    jsonData.forEach((traktList) => {
                                        traktList["images"] = {};
                                        traktList["user"] = {};
                                    });
                                }

                                if (collectedData[normalName] && normalName !== "user-profile") {
                                    // Extend existing data if the file has already been processed (e.g., multiple history files)
                                    try {
                                        const existingData = collectedData[normalName];
                                        const newData = jsonData;
                                        collectedData[normalName] = existingData.concat(newData); // Merge arrays if both existing and new data are arrays

                                    } catch (error) {
                                        failTraktFileImport(`Failed to merge data for ${entry.filename}. Please ensure all files are valid and in the expected format.`);
                                        return; // Stop processing if there's an error merging data
                                    }
                                } else {
                                    collectedData[normalName] = jsonData;
                                }
                            }
                        }

                        if (!collectedData['user-profile'] || Array.isArray(collectedData['user-profile']) ||
                            !traktWantedFiles.some(name => name !== "user-profile" && Array.isArray(collectedData[name]))) {
                            failTraktFileImport("The archive must contain a user-profile JSON file and at least one supported Trakt dataset.");
                            return;
                        }
                        for (const list of collectedData['lists-lists'] || []) {
                            let listItems = [];
                            let foundItems = false;
                            for (const entry of entries) {
                                if (!entry.directory && matchesListExportEntry(entry.filename, list.ids.trakt)) {
                                    const entryBlob = await entry.getData(new zip.BlobWriter());
                                    const items = JSON.parse(await entryBlob.text());
                                    if (!Array.isArray(items)) throw new Error("List items must be an array");
                                    listItems = listItems.concat(items);
                                    foundItems = true;
                                }
                            }
                            if (foundItems) list.items = listItems;
                        }

                        collectedData['file_name'] = file.name; // Store the original file name in the collected data for reference

                        setPrivateDataExpiry("export_expires_at");
                        await setDatabase(collectedData, TRAKT_DB_NAME, TRAKT_DB_VERSION, TRAKT_STORE_NAME, TRAKT_DATA_KEY); // Store the collected data for the migration process to use
                        // Do not write private export data to the browser console.
                        location.reload(); // Reload only after successful processing so form visibility updates correctly
                    } else {
                        failTraktFileImport("The ZIP file is empty. Please upload a valid Trakt data export.");
                    }
                } catch (error) {
                    if (error?.name === "QuotaExceededError") {
                        failTraktFileImport("Browser storage quota was exceeded while importing Trakt data. Clear this site's stored data and try again.", error);
                    } else {
                        failTraktFileImport("Failed to read the ZIP file. Please ensure it's a valid Trakt data export.", error);
                    }
                } finally {
                    if (zipReader) {
                        await zipReader.close();
                    }
                }
            }
        };
        fileInput.click();
    });

    const trakt_data_file = localStorage.getItem("trakt_data_file");
    if (trakt_data_file) {
        traktFileStatus.classList.remove("status-red");
        traktFileStatus.classList.add("status-green");
        traktFileStatusText.textContent = `Trakt Data File (${trakt_data_file})`;
    }
}

const droppedChoice = document.getElementById("sync_dropped_shows_choice");
const droppedPicker = document.getElementById("dropped-picker");
const droppedSearch = document.getElementById("dropped-search");
const droppedOptions = document.getElementById("dropped-options");
const droppedToggle = document.getElementById("toggle-dropped");
function setDroppedExpanded(expanded) {
    droppedPicker.hidden = !expanded;
    droppedToggle.textContent = expanded ? "▼" : "▲";
    droppedToggle.title = expanded ? "Collapse Dropped Shows" : "Expand Dropped Shows";
    droppedToggle.setAttribute("aria-label", droppedToggle.title);
    droppedToggle.setAttribute("aria-expanded", String(expanded));
}
droppedToggle.addEventListener("click", () => setDroppedExpanded(droppedPicker.hidden));
let pickerLoading = false;
async function showDroppedPicker() {
    droppedToggle.hidden = true;
    setDroppedExpanded(false);
    if (!droppedChoice.checked || pickerLoading) return;
    pickerLoading = true;
    try {
        const data = await traktDataHandler();
        if (!data || !droppedChoice.checked || data["hidden-dropped"]?.length) return;
        droppedToggle.hidden = false;
        setDroppedExpanded(true);
        if (droppedOptions.childElementCount) return;
        const titles = new Map();
        for (const rows of Object.values(data)) {
            if (!Array.isArray(rows)) continue;
            for (const row of rows) {
                const show = row?.show;
                if (show?.ids?.tmdb && show.title) titles.set(show.ids.tmdb, show);
            }
        }
        for (const show of [...titles.values()].sort((a,b) => a.title.localeCompare(b.title))) {
            const label = document.createElement("label");
            const box = document.createElement("input");
            box.type = "checkbox";
            box.value = String(show.ids.tmdb);
            label.append(box, " " + show.title + (show.year ? " (" + show.year + ")" : ""));
            droppedOptions.append(label);
        }
        if (!titles.size) droppedOptions.textContent = "No selectable TV series found in this export.";
        filterDroppedTitles();
    } finally { pickerLoading = false; }
}
function filterDroppedTitles() {
    const search = droppedSearch.value.trim().toLowerCase();
    for (const label of droppedOptions.children) {
        label.style.display = label.textContent.toLowerCase().includes(search) ? "block" : "none";
    }
}
droppedSearch.addEventListener("input", filterDroppedTitles);
droppedChoice.addEventListener("change", showDroppedPicker);

const logsButton = document.getElementById("view-logs-button");
const logsContainer = document.querySelector(".migration-logs");
const logsContent = document.getElementById("migration-logs-content");
const downloadLogsButton = document.getElementById("download-logs-button");

function describeLogEntry(entry) {
    if (!entry || typeof entry !== "object") return { level: "info", message: String(entry ?? "") };
    const failed = entry.type === "critical" || entry.level === "error" ||
        (entry.type === "complete" && !migrationOutcome(entry).success);
    const level = failed ? "error" : ["warning", "warn"].includes(entry.level) ? "warning" :
        entry.type === "complete" ? "success" : "info";
    let message = typeof entry.message === "string" ? entry.message : "Migration update";
    if (entry.type === "complete" || entry.type === "critical") {
        message = level === "success" ? "Migration completed successfully." :
            "Migration did not fully succeed. Review the failed stages in PMDB before retrying; your export is preserved.";
    }
    const steps = Array.isArray(entry.steps) ? entry.steps.map(step =>
        `${step.name || step.id || "Stage"}: ${step.status || "unknown"}`).join(" · ") : "";
    return { level, message: message + (steps ? "\n" + steps : "") };
}

function renderMigrationLogs() {
    const raw = localStorage.getItem("migration_logs") || "";
    const onlyIssues = document.getElementById("migration-log-issues").checked;
    const fragment = document.createDocumentFragment();
    let shown = 0;
    for (const line of raw.split("\n").filter(Boolean)) {
        let level = "info";
        try { level = describeLogEntry(JSON.parse(line)).level; } catch {}
        if (onlyIssues && !["error", "warning"].includes(level)) continue;
        // Preserve the original JSON text; only decorate the severity value.
        const highlights = /("level"\s*:\s*")(error|warning|warn|success)(")|(")(success)("\s*:\s*true\b)/gi;
        let cursor = 0;
        for (const match of line.matchAll(highlights)) {
            const word = match[2] || match[5];
            const start = match.index + (match[1] || match[4]).length;
            fragment.appendChild(document.createTextNode(line.slice(cursor, start)));
            const severity = document.createElement("span");
            severity.className = word.toLowerCase() === "error" ? "log-error" :
                word.toLowerCase() === "success" ? "log-success" : "log-warning";
            severity.textContent = word;
            fragment.appendChild(severity);
            cursor = start + word.length;
        }
        fragment.appendChild(document.createTextNode(line.slice(cursor) + "\n"));
        shown++;
    }
    logsContent.replaceChildren(fragment);
    if (!shown) logsContent.textContent = onlyIssues ? "No errors or warnings in the retained logs." : "No logs available.";
}

document.getElementById("migration-log-issues").addEventListener("change", renderMigrationLogs);

logsButton.addEventListener("click", () => {
    if (logsContainer.style.display === "none") {
        renderMigrationLogs();
        logsContainer.style.display = "block";
        logsButton.textContent = "Hide Migration Logs";
    } else {
        logsContainer.style.display = "none";
        logsButton.textContent = "View Migration Logs";
    }
});

downloadLogsButton.addEventListener("click", () => {
    const logs = localStorage.getItem("migration_logs") || "No logs available.";

    if (logs === "No logs available.") {
        alert("No logs available to download.");
        return;
    }

    const blob = new Blob([logs], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `migration_logs_${new Date().toISOString()}.log`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
});

const syncForm = document.getElementById("sync-form");
const syncSubmitButton = document.getElementById("sync-submit");

try {
    const retryOptions = JSON.parse(localStorage.getItem("migration_retry_options") || "null");
    if (Array.isArray(retryOptions)) {
        document.querySelectorAll("input[name='sync_options']").forEach(checkbox => { checkbox.checked = retryOptions.includes(checkbox.id); });
    }
} catch {
    localStorage.removeItem("migration_retry_options");
}

if (droppedChoice.checked) showDroppedPicker();

// Explicitly block native form submit so checked boxes are never appended to the URL.
syncForm.addEventListener("submit", (event) => {
    event.preventDefault();
});

function resumeReviewRows(data, options) {
    function identity(row) {
        const media = row.type === "episode" ? row.show : row.movie;
        const ids = media?.ids || {};
        const id = ids.tmdb ? `tmdb:${ids.tmdb}` : ids.trakt ? `trakt:${ids.trakt}` : null;
        return id ? `${row.type}:${id}:${row.episode?.season ?? ""}:${row.episode?.number ?? ""}` : null;
    }
    const watched = new Set((data["watched-history"] || []).map(identity).filter(Boolean));
    return (data["watched-playback"] || []).map((row, index) => ({row, index})).filter(({row}) =>
        (row.type === "episode" && options.sync_show_resume_points_choice) ||
        (row.type === "movie" && options.sync_movie_resume_points_choice)).map(({row, index}) => {
        const title = (row.show || row.movie)?.title || "Untitled item";
        const episode = row.type === "episode" ? ` S${row.episode?.season}E${row.episode?.number}` : "";
        const warning = watched.has(identity(row)) ? " — Already watched in this export: check whether this pause is still wanted." : "";
        return {index, label: `${title}${episode} — ${row.progress}% — paused ${row.paused_at || "at an unknown date"}${warning}`};
    });
}

async function reviewResumePoints(data, options) {
    const rows = resumeReviewRows(data, options);
    if (!rows.length) return true;
    const dialog = document.getElementById("resume-review");
    const list = document.getElementById("resume-review-items");
    list.replaceChildren();
    for (const row of rows) {
        const label = document.createElement("label");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = true;
        box.value = String(row.index);
        label.append(box, document.createTextNode(row.label));
        list.append(label, document.createElement("br"));
    }
    dialog.returnValue = "cancel";
    const accepted = new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue === "continue"), {once: true}));
    dialog.showModal();
    if (!await accepted) return false;
    const excluded = new Set(Array.from(list.querySelectorAll("input:not(:checked)")).map(box => Number(box.value)));
    data["watched-playback"] = data["watched-playback"].filter((row, index) => !excluded.has(index));
    return true;
}
document.getElementById("resume-review-continue").addEventListener("click", () => document.getElementById("resume-review").close("continue"));
document.getElementById("resume-review-cancel").addEventListener("click", () => document.getElementById("resume-review").close("cancel"));

syncSubmitButton.addEventListener("click", async () => {
    // Build an object where each checkbox id maps to its checked state.
    const syncOptions = Array.from(
        document.querySelectorAll("input[name='sync_options']"),
    ).reduce((options, checkbox) => {
        options[checkbox.id] = checkbox.checked;
        return options;
    }, {});
    if (!Object.values(syncOptions).some(Boolean)) {
        alert("Select at least one migration option.");
        return;
    }
    let collectedData = await traktDataHandler();
    if (!collectedData) {
        return; // Stop the sync process if no valid Trakt data is available
    }

    syncOptions["trakt_data"] = collectedData; // Include the collected Trakt data from local storage in the sync options sent to the server
    delete collectedData["watched-shows"]; // Used locally for title selection.
    delete collectedData["hidden-calendar"]; // Legacy cached exports.
    if (syncOptions.sync_dropped_shows_choice) {
        const rows = collectedData["hidden-dropped"] || [];
        const seen = new Set(rows.map(row => String(row.show?.ids?.tmdb)));
        for (const box of document.querySelectorAll("#dropped-options input:checked")) {
            if (!seen.has(box.value)) rows.push({type: "show", show: {ids: {tmdb: Number(box.value)}}});
        }
        if (!rows.length && !Array.isArray(collectedData["hidden-dropped"])) {
            alert("Select the shows you dropped or deselect Sync Dropped Shows to continue.");
            return;
        }
        collectedData["hidden-dropped"] = rows;
    }
    syncSubmitButton.disabled = true;
    try {
        if (!await reviewResumePoints(collectedData, syncOptions)) return;
    } finally { syncSubmitButton.disabled = false; }
    const syncJson = JSON.stringify(migrationPayload(filterSyncOptions(syncOptions))); // Filter the lists based on the selected checkboxes before sending to the server

    syncSubmitButton.disabled = true;
    fetch("/migrate", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "TraktMigration",
        },
        body: syncJson,
    })
        .then(async (response) => {
            const data = await response.json().catch(() => ({}));
            if ((response.ok || response.status === 409) && data?.success && data.events_url) {
                alert(data.message);

                if (data.events_url) {
                    // Store the events URL in local storage so the status page can use it to listen for updates.
                    localStorage.setItem("events_url", data.events_url);
                    localStorage.removeItem("logs_expires_at");
                    localStorage.removeItem("migration_logs"); // Clear any existing logs
                    localStorage.setItem("migration_progress_percent", "0");
                    localStorage.removeItem("migration_progress");
                    localStorage.removeItem("migration_steps");
                    localStorage.removeItem("migration_retry_options");
                    location.reload(); // Reload the page to update the status indicators
                }
            } else {
                alert(migrationStartError(response.status, data?.detail));
            }
        })
        .catch(() => alert("Could not contact the server. Check the existing migration before retrying."))
        .finally(() => { syncSubmitButton.disabled = false; });
});

function migrationStartError(status, detail) {
    if (status === 503) {
        if (detail === "Migration capacity reached; retry later") {
            return "All migration slots are in use. Your migration was not queued. Please try again in a few minutes.";
        }
        if (typeof detail === "string" && detail.startsWith("Insufficient memory for this export")) {
            return "There is not enough memory available for this export. Try again later or select fewer datasets.";
        }
        if (detail === "Server is shutting down; new migrations are disabled") {
            return "The server is shutting down. Please try again once it is back online.";
        }
        return "The server is busy or temporarily unavailable. Please try again later.";
    }
    switch (status) {
        case 401: return "Your PMDB session is missing or expired. Reconnect PMDB and try again.";
        case 403: return "Request blocked. Open the configured migration website and try again.";
        case 408: return "The upload timed out. Check your connection or select fewer datasets.";
        case 413: return "The selected data exceeds the upload limit. Select fewer datasets and try again.";
        case 422: return "The server could not read the migration data. Check the export and your selections.";
        default: return "Could not confirm that the migration started. Check for an existing migration before retrying.";
    }
}

function mark_form_as_hidden() {
    const form = document.getElementById("sync-form");
    if (form) {
        form.style.display = "none";
    }
}

let sessionStatusKnown = false;
let sessionDeadline = 0;
let sessionHasKey = false;
function renderSessionStatus() {
    if (!sessionStatusKnown) return;
    const remaining = Math.max(0, Math.ceil((sessionDeadline - Date.now()) / 60000));
    document.getElementById("session-status").textContent = remaining ?
        `${sessionHasKey ? "Connected" : "Results-only session; session key removed"}. Session expires in about ${remaining} minute(s). A migration can run for up to 6 hours; stopping waits for any in-flight request.` :
        "No active session. Connect PMDB to start a migration or reconnect to its results.";
}
async function refreshSessionStatus() {
    try {
        const response = await fetch("/auth/status");
        if (!response.ok) throw new Error("Status unavailable");
        const data = await response.json();
        sessionStatusKnown = true;
        sessionDeadline = Date.now() + data.expires_in * 1000;
        sessionHasKey = data.pmdb;
        const dot = document.querySelector(".pmdb-status");
        dot.classList.toggle("status-green", !!data.pmdb);
        dot.classList.toggle("status-red", !data.pmdb);
        document.querySelector(".pmdb-status + .status-text").textContent = data.pmdb ? "PublicMetaDb (Connected)" : data.can_view_results ? "PublicMetaDb (Results only — reconnect to migrate)" : "PublicMetaDb (Not Logged In)";
        document.getElementById("pmdb-logout").hidden = !data.can_view_results;
        document.getElementById("stop-migration").hidden = !data.running_jobs?.length;
        if (!data.pmdb) mark_form_as_hidden();
        renderSessionStatus();
    } catch {
        sessionStatusKnown = false;
        document.getElementById("session-status").textContent = "Unable to check session status. Please retry.";
    }
}
refreshSessionStatus();
setInterval(refreshSessionStatus, 30000);
setInterval(renderSessionStatus, 1000);
const sessionMessage = sessionStorage.getItem("session_action_message");
if (sessionMessage) {
    document.getElementById("session-action-message").textContent = sessionMessage;
    sessionStorage.removeItem("session_action_message");
}

// Hide form if Trakt Data File is not uploaded, as it's required for the migration to work.
const traktFileStatusDot = document.querySelector(".trakt-file-status");
if (traktFileStatusDot) {
    if (traktFileStatusDot.classList.contains("status-red")) {
        mark_form_as_hidden();
    }
}

// Add click behavior to Trakt status text
const traktStatusTextLink = document.querySelector(
    ".trakt-status + .status-text",
);

let server_data = null;
fetch("/status")
    .then((response) => response.json())
    .then((data) => {
        server_data = data;
        if (data.version) {
            console.log(`Server version: ${data.version}`);
        }
        if (data.server_uptime_seconds !== undefined) {
            console.log(`Server uptime (seconds): ${data.server_uptime_seconds}`);

        }


    })
    .catch((error) => {
        console.error("Failed to fetch server status:", error);
        alert("Failed to contact the server. Please try again.");
    });
if (traktStatusTextLink) {
    traktStatusTextLink.addEventListener("click", () => {
        if (server_data && server_data.trakt_url) {
            window.location.href = server_data.trakt_url; // Redirect to the authentication URL
        } else {
            alert("Failed to get authentication URL. Please try again.");
        }
    });
}

const authDialog = document.getElementById("pmdb-auth-dialog");
const apiKeyInput = document.getElementById("pmdb-api-key");
const authError = document.getElementById("pmdb-auth-error");
document.querySelector(".pmdb-status + .status-text").addEventListener("click", () => {
    authError.textContent = "";
    authDialog.showModal();
    apiKeyInput.focus();
});
authDialog.addEventListener("close", () => { apiKeyInput.value = ""; });
document.getElementById("pmdb-auth-cancel").addEventListener("click", () => authDialog.close());
const authSubmit = document.getElementById("pmdb-auth-submit");
authSubmit.addEventListener("click", async () => {
    let apiKey = apiKeyInput.value.trim();
    apiKeyInput.value = "";
    if (!apiKey) return;
    authSubmit.disabled = true;
    try {
        const response = await fetch("/pmdb/auth", {
            method: "POST",
            headers: { "Authorization": `Bearer ${apiKey}`, "X-Requested-With": "TraktMigration" },
        });
        if (response.ok) {
            authDialog.close();
            location.reload();
        } else {
            authError.textContent = response.status === 429
                ? "Too many connection attempts. Wait one minute and retry."
                : "Connection failed. Check your key and PMDB availability, then retry.";
        }
    } catch {
        authError.textContent = "Connection failed. Please try again.";
    } finally {
        apiKey = "";
        authSubmit.disabled = false;
    }
});
document.getElementById("pmdb-logout").addEventListener("click", async () => {
    try {
        const response = await fetch("/pmdb/logout", {method: "POST", headers: {"X-Requested-With": "TraktMigration"}});
        if (!response.ok) throw new Error();
        const data = await response.json();
        sessionStorage.setItem("session_action_message", data.running_jobs ?
            "Disconnected: the session key was removed. Your migration still has its own key until it ends. Reconnect to follow or stop it." :
            "Disconnected: the session key was removed. Your API key remains valid in PMDB until you revoke it there.");
        location.reload();
    } catch { alert("Could not confirm disconnection. Please retry."); }
});
document.getElementById("stop-migration").addEventListener("click", async () => {
    if (!confirm("Stop this migration and remove the session key? A request already sent may still finish. Imported data will remain in PMDB.")) return;
    const button = document.getElementById("stop-migration");
    button.disabled = true;
    try {
        const response = await fetch("/pmdb/stop-and-disconnect", {method: "POST", headers: {"X-Requested-With": "TraktMigration"}});
        if (!response.ok) throw new Error();
        const data = await response.json();
        document.getElementById("session-action-message").textContent = data.message + " Session key removed; results-only access remains until session expiry. The worker releases its copy when it stops.";
        await refreshSessionStatus();
    } catch { alert("Could not confirm the stop request. Reconnect PMDB if your session expired, then retry."); }
    finally { button.disabled = false; }
});

document.getElementById("toggle-all").addEventListener("change", (event) => toggle(event.target));

function updateProgressBar(progress) {
    const progressBar = document.querySelector(".skill-bar");
    if (progressBar) {
        progressBar.style.setProperty("--bar-width", `${progress}%`);
        progressBar.setAttribute("data-progress", progress);
    }
}

function appendMigrationLog(entry) {
    const maxLogChars = 100000;
    const existingLogs = localStorage.getItem("migration_logs") || "";
    let updatedLogs = existingLogs + JSON.stringify(entry) + "\n";

    if (updatedLogs.length > maxLogChars) {
        updatedLogs = updatedLogs.slice(updatedLogs.length - maxLogChars);
        const firstNewline = updatedLogs.indexOf("\n");
        if (firstNewline > -1) {
            updatedLogs = updatedLogs.slice(firstNewline + 1);
        }
    }

    if (!localStorage.getItem("logs_expires_at")) setPrivateDataExpiry("logs_expires_at");
    localStorage.setItem("migration_logs", updatedLogs);


    if (logsContainer.style.display === "block") {
        renderMigrationLogs();
    }
}

// Track intentional page teardown so refresh/navigation does not trigger error cleanup.
let isPageUnloading = false;
window.addEventListener("beforeunload", () => {
    isPageUnloading = true;
});
window.addEventListener("pagehide", () => {
    isPageUnloading = true;
});

const MIGRATION_STAGE_IDS = ["watchlist", "lists", "show-history", "movie-history", "dropped-shows", "show-resume", "movie-resume"];

function renderStageResults(results, activeStep = null) {
    const finished = new Map(results.map(result => [result.id, result]));
    const nextPending = activeStep || (results.length < MIGRATION_STAGE_IDS.length ? results.length + 1 : null);
    MIGRATION_STAGE_IDS.forEach((id, index) => {
        const dot = document.querySelector(`.${id}-status`);
        if (!dot) return;
        const result = finished.get(id);
        const status = result?.status || (index + 1 === nextPending ? "running" : "pending");
        dot.classList.remove("status-red", "status-orange", "status-green");
        dot.classList.add(status === "completed" ? "status-green" : status === "running" ? "status-orange" : "status-red");
        dot.style.opacity = status === "skipped" ? "0.35" : "1";
        dot.title = status;
        dot.setAttribute("aria-label", `${id}: ${status}`);
    });
}

function setMigrationMessage(message) {
    const description = document.querySelector(".skill-content p");
    if (description) description.textContent = message;
}

function restoredMigrationProgress(storedPercent, storedStep) {
    const progress = storedPercent === null ? Number(storedStep || "0") / MIGRATION_STAGE_IDS.length * 100 : Number(storedPercent);
    return Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 0;
}

function migrationOutcome(data) {
    const steps = Array.isArray(data.steps) ? data.steps : [];
    const failed = steps.filter(step => step.status === "failed" || step.status === "not-run");
    const retryOptions = [...new Set([...(data.failed_steps || []), ...failed.map(step => step.option)])].filter(Boolean);
    const success = data.type === "complete" && data.success === true && retryOptions.length === 0;
    return { success, retryOptions, failedNames: failed.map(step => step.name).join(", ") };
}

function isTerminalMigrationEvent(data) {
    // "completed" in a progress event describes one stage, not the whole job.
    return data.type === "complete" || data.type === "critical";
}

async function finalizeMigration(data) {
    if (!isTerminalMigrationEvent(data)) return null;
    const outcome = migrationOutcome(data);
    localStorage.removeItem("events_url");
    localStorage.removeItem("migration_progress");
    localStorage.removeItem("migration_progress_percent");
    localStorage.removeItem("migration_steps");
    if (outcome.success) {
        localStorage.removeItem("migration_retry_options");
        localStorage.removeItem("trakt_data_file");
        await clearDatabase(TRAKT_DB_NAME, TRAKT_DB_VERSION, TRAKT_STORE_NAME, TRAKT_DATA_KEY);
        alert("Migration completed successfully. Check the logs for the stage summary.");
    } else {
        localStorage.setItem("migration_retry_options", JSON.stringify(outcome.retryOptions));
        alert(`${data.type === "critical" ? data.message + " " : ""}Migration did not fully succeed${outcome.failedNames ? ": " + outcome.failedNames : "."} Your export and logs are preserved. Only failed or unrun stages will be selected; review PMDB and reconnect before manually retrying.`);
    }
    return outcome;
}

// Reconnect to the existing job; losing a connection must never start another migration.
const eventsUrl = localStorage.getItem("events_url");
if (eventsUrl) {
    mark_form_as_hidden();
    const storedPercent = localStorage.getItem("migration_progress_percent");
    updateProgressBar(restoredMigrationProgress(storedPercent, localStorage.getItem("migration_progress")));
    let stageResults = [];
    try { stageResults = JSON.parse(localStorage.getItem("migration_steps") || "[]"); } catch {}
    if (!Array.isArray(stageResults)) stageResults = [];
    renderStageResults(stageResults);
    const eventSource = new EventSource(eventsUrl);
    let migrationEnded = false;
    eventSource.addEventListener("session_expired", () => {
        migrationEnded = true;
        eventSource.close();
        setMigrationMessage("Session expired or disconnected. Reconnect PMDB to follow this migration; it continues on the server.");
    });
    eventSource.onmessage = async (event) => {
        if (migrationEnded) return;
        let data;
        try { data = JSON.parse(event.data); } catch { return; }
        appendMigrationLog(data);
        if (Array.isArray(data.steps)) {
            stageResults = data.steps;
            localStorage.setItem("migration_steps", JSON.stringify(stageResults));
            renderStageResults(stageResults);
        }
        if (data.type === "item_progress") {
            const completed = Number(data.completed);
            const total = Number(data.total);
            const step = Number(data.step);
            setMigrationMessage(`${data.stage || data.message || "Processing"}: ${completed} / ${total}`);
            if (Number.isFinite(step) && step >= 1 && step <= MIGRATION_STAGE_IDS.length && total > 0) {
                const percent = (step - 1 + Math.min(1, completed / total)) / MIGRATION_STAGE_IDS.length * 100;
                updateProgressBar(Math.round(percent));
                localStorage.setItem("migration_progress_percent", String(percent));
                renderStageResults(stageResults, step);
            }
        } else if (data.message && data.type === "progress") {
            setMigrationMessage(data.message);
        }
        if (data.progress !== undefined) {
            updateProgressBar(data.progress);
            localStorage.setItem("migration_progress_percent", String(data.progress));
        }
        if (isTerminalMigrationEvent(data)) {
            migrationEnded = true;
            eventSource.close();
            try {
                await finalizeMigration(data);
            } catch {
                alert("Migration ended, but browser cleanup failed. Your logs are still available.");
            }
            location.reload();
        }
    };
    eventSource.onerror = () => {
        if (migrationEnded || isPageUnloading) {
            eventSource.close();
            return;
        }
        // EventSource reconnects automatically; the saved job ID prevents accidental duplicate writes.
        setMigrationMessage(eventSource.readyState === EventSource.CLOSED
            ? "Could not reconnect. Refresh or sign in again to inspect this job; do not submit it again while its result is unknown."
            : "Connection interrupted. Reconnecting to the existing migration...");
    };
} else {
    const progressBar = document.querySelector(".skill-bar");
    const progressBox = document.querySelector(".sf-skill");
    if (progressBar) {
        progressBar.style.setProperty("--bar-width", "0%");
        progressBar.setAttribute("data-progress", "0");
        progressBox.style.display = "none";
    }
    const statusDots = document.querySelector(".progress-dots");
    if (statusDots) statusDots.style.display = "none";
}

})();
