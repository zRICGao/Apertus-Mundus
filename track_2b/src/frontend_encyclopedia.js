/* GM knowledge workspace. No HTML from sources or model responses is executed. */
window.createEncyclopedia = () => {
    const {reactive} = Vue;
    let requestVersion = 0;
    let searchTimer;
    const enc = reactive({
        pages: [], query: '', kind: '', scope: '', state: '', selected: '', detail: null,
        busy: false, loading: false, error: '', message: '', tab: 'read', editing: false,
        draft: null, sourceKey: null, history: [], selectedHistory: null, epoch: '',
        folder: '', scanned: [], scanning: false, trace: null,
        evidenceFocus: null,
        get readParts() {
            return (this.detail?.page.body || '').split(/(\[Evidence:[^\]]+\])/g).map(text =>
                text.startsWith('[Evidence:') ? {evidenceId:text.slice('[Evidence:'.length,-1).trim()} : {text});
        },
        openEvidence(id) { this.evidenceFocus=this.detail.evidence.find(e=>e.evidence_uid===id) || null; this.tab='sources'; },
        get filtered() { return this.pages.filter(p => (!this.kind || p.kind === this.kind) &&
            (!this.scope || p.visibility === this.scope) && (!this.state || p.compile_status === this.state)); },
        get editable() { return this.detail?.sources.find(s => s.source_key === this.selected && /^(document|lore)\//.test(s.source_key)); },
        label(value) { return ({lore:'Lore sources',history:'Events and Memories',entities:'Entities',map:'Location',rules:'Rules',
            pending:'Pending',running:'Compiling',failed:'Compilation failed',done:'Compiled',public:'Public',gm:'GM only',
            mention:'Mention',source:'Source reference',confirmed_relation:'Confirmed relationship',source_revision:'Source updated',
            source_removed:'Source removed',link_review:'Link review',conflict_dismissed:'Dismiss clarification notice'})[value] || value; },
        date(value) { if (!value) return '—'; return new Date(value).toLocaleString(); },
        async act(action) {
            this.busy = true; this.error = ''; this.message = '';
            try { await action(); } catch (error) { this.error = error.message; }
            finally { this.busy = false; }
        },
        async load() {
            const version = ++requestVersion;
            this.loading = true; this.error = '';
            try {
                const result = await WorldAPI.json('/api/wiki?query='+encodeURIComponent(this.query));
                if (version !== requestVersion) return;
                if (this.epoch && this.epoch !== result.epoch) { this.detail = null; this.selected = ''; this.editing = false; }
                this.epoch = result.epoch; this.pages = result.pages;
                if (this.selected && !this.pages.some(p => p.page_key === this.selected)) { this.selected = ''; this.detail = null; }
            } catch (error) { if (version === requestVersion) this.error = error.message; }
            finally { if (version === requestVersion) this.loading = false; }
        },
        search() { clearTimeout(searchTimer); searchTimer = setTimeout(() => this.load(), 180); },
        async select(key) {
            if (this.editing && !confirm('Discard unsaved encyclopedia edits?')) return;
            const version = ++requestVersion;
            this.selected = key; this.detail = null; this.history = []; this.evidenceFocus=null; this.editing = false; this.tab = 'read'; this.error = ''; this.loading = true;
            try {
                const result = await WorldAPI.json('/api/wiki/page?key=' + encodeURIComponent(key));
                if (version !== requestVersion) return;
                if (this.epoch && result.epoch !== this.epoch) { this.selected = ''; await this.load(); return; }
                this.epoch = result.epoch; this.detail = result;
            } catch (error) { if (version === requestVersion) this.error = error.message; }
            finally { if (version === requestVersion) this.loading = false; }
        },
        edit(source = null) {
            this.sourceKey = source?.source_key || null;
            this.draft = {title: source?.title || '', content: source?.content || '', visibility: source?.visibility || 'gm',
                timeline_id: source?.timeline_id ?? null, retrieval_enabled: source ? Boolean(source.enabled) : true,
                expected_revision: source?.revision_uid || null, reason: 'GM edit'};
            this.editing = true; this.error = '';
        },
        async save() {
            await this.act(async () => {
                const result = await WorldAPI.json(this.sourceKey ? '/api/wiki/source?key=' + encodeURIComponent(this.sourceKey) : '/api/wiki/sources',
                    this.draft, this.sourceKey ? 'PUT' : 'POST');
                this.editing = false; await this.load(); await this.select(result.page_key);
                this.message = 'Source saved and queued for compilation.';
            });
        },
        async remove() {
            const source = this.editable;
            if (!source || !confirm('Delete this source and its current encyclopedia references? Checkpoint versions remain restorable through world rollback.')) return;
            await this.act(async () => {
                await WorldAPI.json('/api/wiki/source?key=' + encodeURIComponent(source.source_key) + '&expected_revision=' + source.revision_uid, undefined, 'DELETE');
                this.selected = ''; this.detail = null; await this.load(); this.message = 'Source deleted; associated search references are now invalid.';
            });
        },
        async compile() {
            await this.act(async () => {
                const key = this.detail.sources[0]?.source_key;
                if (!key) return;
                const result = await WorldAPI.json('/api/wiki/synthesize', {page_key:key});
                if (result.status === 'stale') throw new Error(result.message);
                await this.load(); await this.select(key); this.message = 'Compilation complete; original evidence preserved.';
            });
        },
        async upload(event) {
            const file = event.target.files[0]; if (!file) return;
            await this.act(async () => {
                const data = new FormData(); data.append('file',file); data.append('visibility','gm');
                const response = await WorldAPI.checkedRequest('/api/rag/upload',{method:'POST',body:data});
                const result = await response.json(); await this.load(); await this.select('document/'+result.doc_id);
                this.message = 'File saved; awaiting encyclopedia compilation.';
            }); event.target.value = '';
        },
        async showHistory(source) {
            await this.act(async () => {
                this.sourceKey = source.source_key;
                this.history = (await WorldAPI.json('/api/wiki/source/history?key='+encodeURIComponent(source.source_key))).revisions;
                this.selectedHistory = null; this.tab = 'history';
            });
        },
        async restore(rev) {
            const source = this.detail.sources.find(s => s.source_key === this.sourceKey);
            if (!source || !confirm('Create a new revision from this source content? World state will not be rolled back.')) return;
            await this.act(async () => {
                await WorldAPI.json('/api/wiki/source/restore?key='+encodeURIComponent(this.sourceKey),
                    {revision_uid:rev.revision_uid,expected_revision:source.revision_uid});
                const key=this.sourceKey; await this.load(); await this.select(key); this.message='Restored as a new source revision.';
            });
        },
        async review(link, approve) {
            await this.act(async () => { await WorldAPI.json('/api/wiki/links/review',{link_uid:link.link_uid,approve}); await this.select(this.selected); });
        },
        async dismiss(conflict) {
            await this.act(async () => { await WorldAPI.json('/api/wiki/conflicts/dismiss?conflict_uid='+conflict.conflict_uid,{}); await this.select(this.selected); });
        },
        async scan() {
            await this.act(async () => { this.scanned=(await WorldAPI.json('/api/wiki/scan?folder='+encodeURIComponent(this.folder))).files; });
        },
        async importFile(file) {
            await this.act(async () => {
                const result=await WorldAPI.json('/api/wiki/scan/import?folder='+encodeURIComponent(this.folder),
                    {filename:file.filename,content_hash:file.hash,expected_revision:file.expected_revision});
                this.scanned=[]; this.scanning=false; await this.load(); await this.select(result.page_key);
            });
        },
        async explain() {
            await this.act(async () => { this.trace=await WorldAPI.json('/api/wiki/retrieval?query='+encodeURIComponent(this.query)); });
        }
    });
    // Vue may pass event handlers as bare callbacks; retain the reactive receiver.
    for (const key of Object.keys(enc)) if (typeof enc[key] === 'function') enc[key] = enc[key].bind(enc);
    return enc;
};
