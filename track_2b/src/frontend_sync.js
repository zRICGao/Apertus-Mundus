/* Saved world state is shared. Draft input and view settings stay on the device. */
window.WorldSync = (() => {
    function start({kind='workbench', canApply=()=>true, onChange=async()=>{}, onStatus=()=>{}} = {}) {
        const client = WorldAPI.operationKey();
        let applied = null, running = false, stopped = false, timer;
        async function tick() {
            if (running || stopped || document.hidden) return;
            running = true;
            try {
                const response = await fetch(`/api/devices/state?client=${client}&kind=${kind}`, {cache:'no-store'});
                if (!response.ok) throw new Error(response.status===403 ? 'Connection closed; scan the QR code again' : 'Waiting to reconnect');
                const state = await response.json();
                const paused = !canApply();
                onStatus({...state, paused, connected:true});
                if (!paused && state.revision !== applied) {
                    const accepted = await onChange(state);
                    if (accepted !== false) applied = state.revision;
                }
            } catch(error) {onStatus({connected:false, error:error.message});}
            finally {running=false;}
        }
        const wake = () => {if (!document.hidden) tick();};
        const hide = event => {if (!event.persisted) stop();};
        const show = event => {if (event.persisted) tick();};
        function stop() {
            stopped=true; clearInterval(timer);
            document.removeEventListener('visibilitychange',wake);
            window.removeEventListener('pagehide',hide);
            window.removeEventListener('pageshow',show);
        }
        timer=setInterval(tick,2500);
        document.addEventListener('visibilitychange',wake);
        window.addEventListener('pagehide',hide);
        window.addEventListener('pageshow',show);
        tick();
        return {stop, refresh:tick};
    }
    return {start};
})();
