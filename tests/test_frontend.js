const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const functionNames = ['exportBasename', 'normaliseExportEntryName', 'matchesListExportEntry',
    'migrationStartError', 'migrationPayload', 'filterSyncOptions', 'restoredMigrationProgress', 'migrationOutcome', 'isTerminalMigrationEvent', 'finalizeMigration', 'renderStageResults', 'clearPrivateLocalData', 'setPrivateDataExpiry', 'expirePrivateLocalData'];
const extracted = functionNames.map(name => {
    const match = source.match(new RegExp('^(?:async )?function ' + name + '\\([^]*?^}', 'm'));
    assert.ok(match, `The application exposes ${name}`);
    return match[0];
}).join('\n');
const datasetNames = source.match(/const traktWantedFiles = \[[^]*?\];/)[0];
const stageIds = source.match(/const MIGRATION_STAGE_IDS = \[[^]*?\];/)[0];

function storageFixture() {
    const state = { closed: 0, transaction: null };
    const db = {
        close: () => { state.closed += 1; },
        transaction: () => {
            state.transaction = { objectStore: () => ({ put: () => ({}), delete: () => ({}) }) };
            return state.transaction;
        },
    };
    const context = vm.createContext({ openDatabase: async () => db });
    for (const name of ['setDatabase', 'clearDatabase']) {
        vm.runInContext(source.match(new RegExp('^async function ' + name + '\\([^]*?^}', 'm'))[0], context);
    }
    return { context, state };
}

test('export writes and cleanup finish only after the storage transaction commits', async () => {
    for (const operation of ['setDatabase', 'clearDatabase']) {
        const { context, state } = storageFixture();
        let settled = false;
        const result = context[operation]({}, 'fixture', 1, 'fixture', 'fixture').then(() => { settled = true; });
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(settled, false);
        state.transaction.oncomplete();
        await result;
        assert.equal(settled, true);
        assert.equal(state.closed, 1);
    }
});

test('an aborted export transaction fails instead of allowing a reload', async () => {
    const { context, state } = storageFixture();
    const result = context.setDatabase({}, 'fixture', 1, 'fixture', 'fixture');
    await new Promise(resolve => setImmediate(resolve));
    state.transaction.error = new Error('synthetic storage failure');
    state.transaction.onabort();
    await assert.rejects(result, /synthetic storage failure/);
    assert.equal(state.closed, 1);
});

function browserFixture(initialStorage = {}, checkboxes = []) {
    const storage = new Map(Object.entries(initialStorage));
    const state = { clearCalls: 0, alerts: [], dots: new Map() };
    const context = vm.createContext({
        document: {
            querySelectorAll: () => checkboxes,
            querySelector: selector => state.dots.get(selector) || null,
        },
        localStorage: {
            getItem: key => storage.has(key) ? storage.get(key) : null,
            setItem: (key, value) => storage.set(key, String(value)),
            removeItem: key => storage.delete(key),
        },
        alert: message => state.alerts.push(message),
        clearDatabase: async () => { state.clearCalls += 1; },
        TRAKT_DB_NAME: 'fixture', TRAKT_DB_VERSION: 1, TRAKT_STORE_NAME: 'fixture', TRAKT_DATA_KEY: 'fixture',
    });
    vm.runInContext(datasetNames + '\n' + stageIds + '\n' + extracted, context);
    return { context, storage, state };
}

function streamFixture() {
    const fixture = browserFixture({ events_url: '/fixture/events', trakt_data_file: 'fixture.zip', migration_logs: 'fixture-log' });
    const { context, state } = fixture;
    Object.assign(state, { closeCalls: 0, reloadCalls: 0, progress: [], messages: [] });
    Object.assign(context, {
        mark_form_as_hidden: () => {},
        updateProgressBar: value => state.progress.push(value),
        appendMigrationLog: entry => state.messages.push(entry),
        setMigrationMessage: () => {},
        isPageUnloading: false,
        location: { reload: () => { state.reloadCalls += 1; } },
        EventSource: class {
            addEventListener(name, handler) { this[name] = handler; }
            constructor(url) { this.url = url; state.stream = this; }
            close() { state.closeCalls += 1; }
        },
    });
    // Execute the real EventSource setup and callback, rather than duplicating
    // the application's dispatch condition in this regression test.
    vm.runInContext(source.slice(source.indexOf('const eventsUrl = localStorage.getItem("events_url");'), source.lastIndexOf('})();')), context);
    return fixture;
}

test('nested ZIP entries and split dataset names normalize to supported datasets', () => {
    const { context } = browserFixture();
    assert.equal(context.normaliseExportEntryName('export-folder/watched-playback.json'), 'watched-playback');
    assert.equal(context.normaliseExportEntryName('export.folder\\watched-history-2.JSON'), 'watched-history');
    assert.equal(context.normaliseExportEntryName('folder/user-profile.json'), 'user-profile');
    assert.equal(context.normaliseExportEntryName('folder/notes.txt'), null);
    assert.equal(context.normaliseExportEntryName('folder/lists-list-123.json'), null);
});

test('list item chunks match the exact list ID inside nested folders', () => {
    const { context } = browserFixture();
    assert.equal(context.matchesListExportEntry('folder/lists-list-12-1.json', 12), true);
    assert.equal(context.matchesListExportEntry('folder/lists-list-12_2.json', 12), true);
    assert.equal(context.matchesListExportEntry('folder/lists-list-123.json', 12), false);
    assert.equal(context.matchesListExportEntry('folder/lists-list-12.txt', 12), false);
});

test('a watchlist-only export is filtered without fabricating missing playback or history', () => {
    const { context } = browserFixture();
    const options = { sync_watchlist_choice: true, trakt_data: { 'lists-watchlist': [{ type: 'movie' }] } };
    assert.equal(context.filterSyncOptions(options), options);
    assert.equal(options.trakt_data['lists-watchlist'].length, 1);
    assert.equal(Object.hasOwn(options.trakt_data, 'watched-playback'), false);
    assert.equal(Object.hasOwn(options.trakt_data, 'watched-history'), false);
});

test('an absent selected playback dataset is left absent for explicit server failure', () => {
    const { context } = browserFixture();
    const options = { sync_movie_resume_points_choice: true, trakt_data: {} };
    context.filterSyncOptions(options);
    assert.equal(Object.hasOwn(options.trakt_data, 'watched-playback'), false);
});

test('media and list selections filter only the requested records', () => {
    const { context } = browserFixture({}, [{ checked: true, id: 'list-2' }, { checked: false, id: 'list-1' }]);
    const options = { sync_lists_choice: true, sync_show_resume_points_choice: true,
        trakt_data: { 'watched-playback': [{ type: 'movie' }, { type: 'episode' }], 'lists-lists': [{ ids: { trakt: 1 } }, { ids: { trakt: 2 } }] } };
    context.filterSyncOptions(options);
    assert.deepEqual(options.trakt_data['watched-playback'], [{ type: 'episode' }]);
    assert.deepEqual(options.trakt_data['lists-lists'], [{ ids: { trakt: 2 } }]);
});

test('legacy six-of-seven progress restores to86 percent rather than100', () => {
    const { context } = browserFixture();
    assert.equal(Math.round(context.restoredMigrationProgress(null, '6')), 86);
    assert.equal(context.restoredMigrationProgress(null, '7'), 100);
    assert.equal(context.restoredMigrationProgress('0', '7'), 0);
    assert.equal(context.restoredMigrationProgress('bad', '7'), 0);
});

test('partial completion preserves export and selects only failed stages for manual retry', async () => {
    const { context, storage, state } = browserFixture({ events_url: '/fixture', trakt_data_file: 'fixture.zip', migration_logs: 'fixture-log' });
    await context.finalizeMigration({ type: 'complete', success: false, steps: [
        { name: 'watchlist', option: 'sync_watchlist_choice', status: 'completed' },
        { name: 'show resume points', option: 'sync_show_resume_points_choice', status: 'failed' },
    ] });
    assert.equal(state.clearCalls, 0);
    assert.equal(storage.get('trakt_data_file'), 'fixture.zip');
    assert.equal(storage.get('migration_logs'), 'fixture-log');
    assert.deepEqual(JSON.parse(storage.get('migration_retry_options')), ['sync_show_resume_points_choice']);
    assert.equal(storage.has('events_url'), false);
    assert.match(state.alerts[0], /did not fully succeed/);
});

test('a contradictory success flag cannot hide a failed stage', () => {
    const { context } = browserFixture();
    const outcome = context.migrationOutcome({ type: 'complete', success: true, steps: [{ option: 'sync_lists_choice', status: 'failed' }] });
    assert.equal(outcome.success, false);
});

test('completed stages keep the job, export and logs until an explicit terminal event', async () => {
    const { context, storage, state } = browserFixture({ events_url: '/fixture', trakt_data_file: 'fixture.zip', migration_logs: 'fixture-log' });
    for (let step = 1; step <= 7; step += 1) {
        const event = { type: 'progress', status: 'completed', step, progress: Math.round(step / 7 * 100), steps: [{ status: 'completed' }] };
        assert.equal(context.isTerminalMigrationEvent(event), false);
        assert.equal(context.migrationOutcome(event).success, false);
        assert.equal(await context.finalizeMigration(event), null);
        assert.equal(storage.get('events_url'), '/fixture');
        assert.equal(storage.get('trakt_data_file'), 'fixture.zip');
    }
    assert.equal(state.clearCalls, 0);
    assert.equal(state.alerts.length, 0);
    await context.finalizeMigration({ type: 'complete', success: true, steps: [{ status: 'completed' }] });
    assert.equal(state.clearCalls, 1);
    assert.equal(storage.has('events_url'), false);
});

test('the live event handler stays connected through completed stages and finalizes once', async () => {
    const { storage, state } = streamFixture();
    const dispatch = entry => state.stream.onmessage({ data: JSON.stringify(entry) });
    assert.equal(state.stream.url, '/fixture/events');
    for (let step = 1; step <= 7; step += 1) {
        await dispatch({ type: 'progress', status: 'completed', step, progress: Math.round(step / 7 * 100), steps: [{ status: 'completed' }] });
        assert.equal(state.closeCalls, 0);
        assert.equal(state.reloadCalls, 0);
        assert.equal(state.clearCalls, 0);
        assert.equal(state.alerts.length, 0);
        assert.equal(storage.get('events_url'), '/fixture/events');
        assert.equal(storage.get('trakt_data_file'), 'fixture.zip');
    }
    assert.equal(state.progress.at(-1), 100);
    const complete = { type: 'complete', success: true, steps: [{ status: 'completed' }] };
    await dispatch(complete);
    assert.equal(state.closeCalls, 1);
    assert.equal(state.reloadCalls, 1);
    assert.equal(state.clearCalls, 1);
    assert.equal(state.alerts.length, 1);
    assert.equal(storage.has('events_url'), false);
    assert.equal(storage.has('trakt_data_file'), false);
    const messageCount = state.messages.length;
    await dispatch(complete);
    await dispatch({ type: 'progress', status: 'completed', progress: 14 });
    assert.equal(state.closeCalls, 1);
    assert.equal(state.reloadCalls, 1);
    assert.equal(state.clearCalls, 1);
    assert.equal(state.alerts.length, 1);
    assert.equal(state.messages.length, messageCount);
    assert.equal(state.progress.at(-1), 100);
});

test('terminal success requires an explicit success true flag', async () => {
    const { context, storage, state } = browserFixture({ trakt_data_file: 'fixture.zip' });
    assert.equal(context.isTerminalMigrationEvent({ type: 'critical' }), true);
    assert.equal(context.isTerminalMigrationEvent({ type: 'complete' }), true);
    assert.equal(context.isTerminalMigrationEvent({ status: 'completed' }), false);
    await context.finalizeMigration({ type: 'complete' });
    assert.equal(state.clearCalls, 0);
    assert.equal(storage.get('trakt_data_file'), 'fixture.zip');
});

test('critical failures keep export and all unrun selected stages available', async () => {
    const { context, storage, state } = browserFixture({ trakt_data_file: 'fixture.zip' });
    await context.finalizeMigration({ type: 'critical', success: false, failed_steps: ['sync_movie_resume_points_choice'] });
    assert.equal(state.clearCalls, 0);
    assert.equal(storage.get('trakt_data_file'), 'fixture.zip');
    assert.deepEqual(JSON.parse(storage.get('migration_retry_options')), ['sync_movie_resume_points_choice']);
});

test('only successful completion removes export data and preserves logs', async () => {
    const { context, storage, state } = browserFixture({ trakt_data_file: 'fixture.zip', migration_logs: 'fixture-log' });
    await context.finalizeMigration({ type: 'complete', success: true, steps: [{ status: 'completed' }] });
    assert.equal(state.clearCalls, 1);
    assert.equal(storage.has('trakt_data_file'), false);
    assert.equal(storage.get('migration_logs'), 'fixture-log');
});

function privacyFixture(initial) {
    const fixture = browserFixture(initial);
    vm.runInContext('const LOCAL_DATA_TTL_MS = 86400000; const PRIVATE_LOCAL_KEYS = ["trakt_data", "trakt_data_file", "migration_logs", "migration_retry_options"];', fixture.context);
    return fixture;
}

test('expired browser data is removed but a running job remains reconnectable', async () => {
    const {context, storage, state} = privacyFixture({private_data_expires_at: '1', trakt_data_file: 'test.zip', migration_logs: 'private', events_url: '/job/events'});
    assert.equal(await context.expirePrivateLocalData(), true);
    assert.equal(state.clearCalls, 1);
    assert.equal(storage.has('migration_logs'), false);
    assert.equal(storage.has('trakt_data_file'), false);
    assert.equal(storage.get('events_url'), '/job/events');
});

test('unexpired exports remain available for retry and legacy storage gets a deadline', async () => {
    const {context, storage, state} = privacyFixture({trakt_data_file: 'test.zip'});
    assert.equal(await context.expirePrivateLocalData(), false);
    assert.ok(Number(storage.get('export_expires_at')) > Date.now());
    assert.equal(await context.expirePrivateLocalData(), false);
    assert.equal(state.clearCalls, 0);
});

test('session revocation closes the stream without forgetting or resubmitting the job', () => {
    const {storage, state} = streamFixture();
    state.stream.session_expired();
    assert.equal(state.closeCalls, 1);
    assert.equal(storage.get('events_url'), '/fixture/events');
    assert.equal(state.clearCalls, 0);
});

test('new export does not extend old logs', async () => {
    const {context, storage, state} = privacyFixture({logs_expires_at: '1', migration_logs: 'private', trakt_data_file: 'fresh'});
    context.setPrivateDataExpiry('export_expires_at');
    await context.expirePrivateLocalData();
    assert.equal(storage.has('migration_logs'), false);
    assert.equal(storage.get('trakt_data_file'), 'fresh');
    assert.equal(state.clearCalls, 0);
});

test('dropped-only payload excludes profile, filename, notes and unrelated datasets', () => {
    const {context} = browserFixture();
    const payload = context.migrationPayload({sync_dropped_shows_choice: true, trakt_data: {
        file_name: 'private', 'user-profile': {username: 'private'}, 'watched-history': [],
        'hidden-dropped': [{type: 'show', note: 'private', show: {title: 'private', ids: {tmdb: 7, imdb: 'private'}}}]
    }});
    assert.deepEqual(JSON.parse(JSON.stringify(payload.trakt_data)), {'hidden-dropped': [{type:'show', show:{ids:{tmdb:7}}}]});
});

test('migration admission failures explain the relevant recovery action', () => {
    const {context} = browserFixture();
    assert.match(context.migrationStartError(503, 'Migration capacity reached; retry later'), /not queued/);
    assert.match(context.migrationStartError(503, 'Insufficient memory for this export; retry later or select fewer datasets'), /fewer datasets/);
    assert.match(context.migrationStartError(503), /temporarily unavailable/);
    assert.match(context.migrationStartError(401), /Reconnect PMDB/);
    assert.match(context.migrationStartError(413), /upload limit/);
});
