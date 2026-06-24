// ════════════════════════════════════════════════════════════════════════════
// VERSIÓN ANTERIOR (comentada para referencia histórica)
// Esta fue la primera implementación — se dejó como backup.
// Problema: el patch() de Odoo no tenía efecto porque root.js captura la
// referencia a selfOrderIndex ANTES de que nuestro patch corra (mismo bundle,
// sin garantía de orden). El setup() nunca ejecutaba, el subscriber nunca se
// registraba, y el video/navegación nunca funcionaban.
// La solución correcta está más abajo: asignación directa al prototipo.
// ════════════════════════════════════════════════════════════════════════════

// /** @odoo-module **/

// console.log("[vending_kiosk_ui] routes.js module loaded");

// import { registry } from "@web/core/registry";
// import { reactive, onMounted, onWillUnmount } from "@odoo/owl";
// import { rpc } from "@web/core/network/rpc";
// import { patch } from "@web/core/utils/patch";
// import { selfOrderIndex } from "@pos_self_order/app/self_order_index";
// import { useService } from "@web/core/utils/hooks";
// import { VendingProcessingScreen } from "./screens/vending_processing_screen/vending_processing_screen";
// import { VendingSuccessScreen } from "./screens/vending_success_screen/vending_success_screen";
// import { VendingPaymentSuccessScreen } from "./screens/vending_payment_success_screen/vending_payment_success_screen";
// import { VendingErrorScreen } from "./screens/vending_error_screen/vending_error_screen";
// import { VendingOutOfServiceScreen } from "./screens/vending_out_of_service_screen/vending_out_of_service_screen";

// // ════════════════════════════════════════════════════════════════════════════
// // 1) Service `vending_product` registrado in-place.
// //    Sostiene el polling/bus de productos vending fuera del lifecycle de los
// //    componentes, así sobrevive a re-renders del root y al cartel "Hey, looks
// //    like..." nativo si llegara a aparecer durante un instante.
// // ════════════════════════════════════════════════════════════════════════════

// const POLL_FAST_MS = 3000;

// export const vendingProductService = {
//     dependencies: ["bus_service"],

//     start(env, { bus_service }) {
//         console.log("[vending_product] service factory start() called");
//         const state = reactive({
//             ready: false,
//             posConfigId: null,
//             availableIds: [],
//             productSlots: {},
//             productMinSlotCode: {},
//             productMeta: {},
//             machineFaultBlocked: false,
//             machineHasFaultBlockedSlots: false,
//             machineFaultBlockedSlotsCount: 0,
//             lastPollAt: null,
//             lastPollOk: false,
//         });

//         let pollTimer = null;
//         let busChannel = null;
//         let currentHash = "";
//         let started = false;
//         let hasReconciledOnce = false; // primera vez que pollNow trae datos del servidor
//         let lastKnownRefreshToken = null; // botón "refrescar pantalla del kiosko"
//         let busListenerAttached = false;
//         let visibilityListenerAttached = false;
//         let selfOrderRef = null;

//         function _toTemplateId(product) {
//             if (!product) return null;
//             const maybeId = product.product_tmpl_id?.id ?? product.id;
//             const numericId = Number(maybeId);
//             return Number.isFinite(numericId) ? numericId : null;
//         }

//         function _iterProductCandidates(visitor) {
//             if (!selfOrderRef) return;
//             const visited = new Set();
//             const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

//             const visit = (product) => {
//                 if (!product || typeof product !== "object") return;
//                 const looksLikeProduct = (
//                     hasOwn(product, "display_name")
//                     || hasOwn(product, "list_price")
//                     || hasOwn(product, "public_description")
//                     || hasOwn(product, "product_tmpl_id")
//                 );
//                 if (!looksLikeProduct) return;
//                 if (visited.has(product)) return;
//                 visited.add(product);
//                 visitor(product);
//             };

//             const modelContainers = [];
//             const models = selfOrderRef?.models;

//             if (Array.isArray(models)) {
//                 modelContainers.push(models);
//             } else if (models && typeof models === "object") {
//                 const productModelKeys = new Set([
//                     "product.template",
//                     "product_template",
//                     "product.product",
//                     "product_product",
//                 ]);
//                 for (const [key, value] of Object.entries(models)) {
//                     if (!productModelKeys.has(key)) continue;
//                     if (Array.isArray(value)) {
//                         modelContainers.push(value);
//                     } else if (value && typeof value === "object") {
//                         modelContainers.push(Object.values(value));
//                     }
//                 }
//             }

//             const extraCollections = [
//                 selfOrderRef?.products,
//                 selfOrderRef?.productTemplates,
//                 selfOrderRef?.productById && Object.values(selfOrderRef.productById),
//             ];

//             for (const collection of [...modelContainers, ...extraCollections]) {
//                 if (!collection) continue;
//                 if (Array.isArray(collection)) {
//                     for (const item of collection) {
//                         visit(item);
//                     }
//                 }
//             }

//             visit(selfOrderRef?.selectedVendingProduct);
//         }

//         function _applyMetaToProduct(product, meta) {
//             const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
//             const normalizeOdooDateTime = (value) => {
//                 if (!value || typeof value !== "string") return false;
//                 const trimmed = value.trim();
//                 if (!trimmed) return false;
//                 const normalized = trimmed.replace("T", " ").split(".")[0];
//                 return normalized || false;
//             };
//             if (!product || !meta) return;

//             if (hasOwn(meta, "display_name")) {
//                 product.display_name = meta.display_name || "";
//                 if (hasOwn(product, "name")) {
//                     product.name = meta.display_name || product.name || "";
//                 }
//             }
//             if (hasOwn(meta, "public_description")) {
//                 product.public_description = meta.public_description || false;
//             }
//             if (hasOwn(meta, "write_date")) {
//                 product.write_date = normalizeOdooDateTime(meta.write_date);
//             }
//             if (hasOwn(meta, "price")) {
//                 const parsedPrice = Number(meta.price);
//                 if (Number.isFinite(parsedPrice)) {
//                     product.list_price = parsedPrice;
//                     if (hasOwn(product, "lst_price")) {
//                         product.lst_price = parsedPrice;
//                     }
//                     product._vending_price_override = parsedPrice;
//                 }
//             }
//         }

//         function applyProductMeta(productMeta) {
//             if (!productMeta || typeof productMeta !== "object") return;

//             for (const key of Object.keys(state.productMeta)) {
//                 delete state.productMeta[key];
//             }
//             Object.assign(state.productMeta, productMeta);

//             _iterProductCandidates((product) => {
//                 const templateId = _toTemplateId(product);
//                 if (!templateId) return;
//                 const meta = productMeta[templateId] || productMeta[String(templateId)];
//                 if (!meta) return;
//                 _applyMetaToProduct(product, meta);
//             });

//             const selected = selfOrderRef?.selectedVendingProduct;
//             const selectedTemplateId = _toTemplateId(selected);
//             if (selectedTemplateId) {
//                 const selectedMeta = productMeta[selectedTemplateId] || productMeta[String(selectedTemplateId)];
//                 if (selectedMeta) {
//                     _applyMetaToProduct(selected, selectedMeta);
//                 }
//             }
//         }

//         let beforeMutateHook = null;

//         function setBeforeMutate(hook) {
//             beforeMutateHook = typeof hook === "function" ? hook : null;
//         }

//         // Suscripción independiente del ciclo de render OWL — el root no
//         // re-renderiza ante cambios de vendingState (su template no lee el
//         // proxy reactivo), así que useEffect no dispararía. Acá llamamos a
//         // los suscriptores DESPUÉS de cada updateProducts, sea quien sea.
//         const subscribers = new Set();
//         function subscribe(cb) {
//             if (typeof cb !== "function") return () => {};
//             subscribers.add(cb);
//             return () => subscribers.delete(cb);
//         }
//         function notifySubscribers() {
//             for (const cb of subscribers) {
//                 try { cb(); } catch (err) {
//                     console.warn("[vending_product] subscriber error:", err);
//                 }
//             }
//         }

//         // Rutas en las que NUNCA forzamos un reload (pago en curso).
//         const HARD_REDIRECT_BLOCKED_ROUTES = [
//             "vending-processing",
//             "vending-process",
//             "vending-success",
//             "vending-payment-success",
//             "vending-error",
//         ];

//         function _hardRedirectIfNeeded(prevDegraded, prevAvailable, nextDegraded, nextAvailable) {
//             if (typeof window === "undefined" || !state.posConfigId) return;
//             const path = window.location.pathname || "";
//             const isBlockedRoute = HARD_REDIRECT_BLOCKED_ROUTES.some(r => path.endsWith(`/${r}`));
//             if (isBlockedRoute) return;

//             const onOOS = path.endsWith("/vending-out-of-service");
//             const transitionedToHealthy = prevDegraded && !nextDegraded;
//             const transitionedToDegraded = !prevDegraded && nextDegraded;

//             // Primera vez que tenemos datos del servidor: reconciliar URL
//             // incondicionalmente (sin importar transición), porque venimos de
//             // un default ambiguo (vacío) que no refleja el estado real.
//             const firstReconcile = !hasReconciledOnce;
//             hasReconciledOnce = true;

//             if ((transitionedToHealthy || firstReconcile) && !nextDegraded && onOOS) {
//                 const target = `/pos-self/${state.posConfigId}`;
//                 console.log(`[vending_product] HARD REDIRECT (healthy) -> ${target}`);
//                 window.location.assign(target);
//                 return;
//             }
//             if ((transitionedToDegraded || firstReconcile) && nextDegraded && !onOOS) {
//                 const target = `/pos-self/${state.posConfigId}/vending-out-of-service`;
//                 console.log(`[vending_product] HARD REDIRECT (degraded) -> ${target}`);
//                 window.location.assign(target);
//             }
//         }

//         function updateProducts(newIds, newSlots, newProductMinSlotCode, newProductMeta, machineState = null) {
//             // Snapshot del state PREVIO para detectar transiciones blocked↔healthy.
//             const prevDegraded = Boolean(state.machineFaultBlocked) || state.availableIds.length === 0;
//             const prevAvailableLen = state.availableIds.length;

//             // Hook sincrónico ANTES de tocar el state reactivo. routes.js lo
//             // usa para navegar fuera de /products cuando el catálogo se va a
//             // vaciar — así ProductListPage no llega a re-renderizar con lista
//             // vacía y se evita el crash de category_scrollspy_hook.
//             if (beforeMutateHook) {
//                 try {
//                     beforeMutateHook({
//                         nextAvailableIds: newIds || [],
//                         nextMachineFaultBlocked: !!(machineState && machineState.machineFaultBlocked),
//                     });
//                 } catch (err) {
//                     console.warn("[vending_product] beforeMutate hook failed:", err);
//                 }
//             }
//             state.availableIds.splice(0, state.availableIds.length, ...newIds);
//             if (newSlots) {
//                 for (const key of Object.keys(state.productSlots)) {
//                     delete state.productSlots[key];
//                 }
//                 Object.assign(state.productSlots, newSlots);
//             }
//             if (newProductMinSlotCode) {
//                 for (const key of Object.keys(state.productMinSlotCode)) {
//                     delete state.productMinSlotCode[key];
//                 }
//                 Object.assign(state.productMinSlotCode, newProductMinSlotCode);
//             }
//             if (newProductMeta) {
//                 applyProductMeta(newProductMeta);
//             }
//             if (machineState && typeof machineState === "object") {
//                 state.machineFaultBlocked = Boolean(machineState.machineFaultBlocked);
//                 state.machineHasFaultBlockedSlots = Boolean(machineState.machineHasFaultBlockedSlots);
//                 state.machineFaultBlockedSlotsCount = Number(machineState.machineFaultBlockedSlotsCount || 0);
//             }
//             notifySubscribers();

//             // Fallback bruto: si por cualquier razón los hooks/subscribe no
//             // navegan (race de patch, useState wrapper, lo que sea), forzamos
//             // un window.location.assign para garantizar que el frontend
//             // refleje el cambio sin reload manual.
//             const nextDegraded = Boolean(state.machineFaultBlocked) || state.availableIds.length === 0;
//             _hardRedirectIfNeeded(prevDegraded, prevAvailableLen, nextDegraded, state.availableIds.length);
//         }

//         async function pollNow() {
//             if (!state.posConfigId) return;
//             console.log(`[vending_product] pollNow tick (configId=${state.posConfigId})`);
//             try {
//                 const resp = await rpc("/v1/vending/products/poll", {
//                     pos_config_id: state.posConfigId,
//                     current_hash: currentHash,
//                 });
//                 state.lastPollAt = Date.now();

//                 if (resp.error) {
//                     state.lastPollOk = false;
//                     console.warn("[vending_product] poll error from server:", resp.error);
//                     return;
//                 }

//                 state.lastPollOk = true;
//                 currentHash = resp.hash || "";

//                 // Detectar pulsación del botón "Refrescar pantalla del kiosko"
//                 // del form de la máquina: el backend incrementa kiosk_refresh_token,
//                 // entra en el hash y nos llega acá. Forzamos un reload duro a la raíz.
//                 const incomingToken = Number.isFinite(resp.kiosk_refresh_token)
//                     ? Number(resp.kiosk_refresh_token)
//                     : null;
//                 if (incomingToken !== null) {
//                     if (lastKnownRefreshToken === null) {
//                         // Primera vez que vemos el token: lo registramos sin redirect.
//                         lastKnownRefreshToken = incomingToken;
//                     } else if (incomingToken > lastKnownRefreshToken) {
//                         const path = window.location.pathname || "";
//                         const isBlockedRoute = HARD_REDIRECT_BLOCKED_ROUTES.some(r => path.endsWith(`/${r}`));
//                         if (!isBlockedRoute) {
//                             const target = `/pos-self/${state.posConfigId}`;
//                             console.log(
//                                 `[vending_product] HARD REDIRECT (manual refresh button, token ${lastKnownRefreshToken}->${incomingToken}) -> ${target}`
//                             );
//                             lastKnownRefreshToken = incomingToken;
//                             window.location.assign(target);
//                             return;
//                         }
//                         lastKnownRefreshToken = incomingToken;
//                     }
//                 }

//                 if (!resp.changed) return;

//                 console.log(
//                     `[vending_product] poll change ids=${(resp.product_ids || []).length} ` +
//                     `blocked=${!!resp.machine_fault_blocked}`
//                 );

//                 updateProducts(
//                     resp.product_ids || [],
//                     resp.product_slots || null,
//                     resp.product_min_slot_code || null,
//                     resp.product_meta || null,
//                     {
//                         machineFaultBlocked: resp.machine_fault_blocked,
//                         machineHasFaultBlockedSlots: resp.machine_has_fault_blocked_slots,
//                         machineFaultBlockedSlotsCount: resp.machine_fault_blocked_slots_count,
//                     },
//                 );
//             } catch (err) {
//                 state.lastPollOk = false;
//                 console.warn("[vending_product] poll network error:", err);
//             }
//         }

//         // Polling robusto basado en setInterval fijo a la cadencia rápida
//         // (3s). El endpoint /poll devuelve `changed: false` rapidísimo cuando
//         // no hay cambios (compara hash); no necesitamos cadencia adaptativa.
//         // Un solo timer global imposible de "olvidar" como pasaba con la
//         // cadena de setTimeout recursivos.
//         function _stopPolling() {
//             if (pollTimer) {
//                 clearInterval(pollTimer);
//                 pollTimer = null;
//             }
//         }

//         function _ensurePolling() {
//             if (pollTimer) return;
//             console.log(`[vending_product] interval armed every ${POLL_FAST_MS}ms`);
//             pollTimer = setInterval(() => {
//                 pollNow();
//             }, POLL_FAST_MS);
//         }

//         function onBusNotification({ detail: notifications }) {
//             if (!notifications || !Array.isArray(notifications) || !busChannel) return;
//             const relevant = notifications.filter(n => n.payload?.channel === busChannel);
//             if (!relevant.length) return;
//             for (const notif of relevant) {
//                 const msg = notif.payload;
//                 if (msg.type !== "vending_products_update") continue;
//                 console.log(
//                     `[vending_product] bus notif ids=${(msg.all_available_ids || []).length} ` +
//                     `blocked=${!!msg.machine_fault_blocked}`
//                 );
//                 updateProducts(msg.all_available_ids || [], null, null, null, {
//                     machineFaultBlocked: msg.machine_fault_blocked,
//                     machineHasFaultBlockedSlots: msg.machine_has_fault_blocked_slots,
//                     machineFaultBlockedSlotsCount: msg.machine_fault_blocked_slots_count,
//                 });
//                 pollNow();
//             }
//         }

//         function onVisibilityChange() {
//             if (typeof document === "undefined" || document.visibilityState !== "visible") return;
//             if (!started) return;
//             console.log("[vending_product] visibility -> visible, re-polling");
//             pollNow();
//             _ensurePolling();
//         }

//         function startService({ posConfigId, selfOrder, initial }) {
//             if (started) return;
//             if (!posConfigId) {
//                 console.warn("[vending_product] start aborted: no posConfigId");
//                 return;
//             }
//             started = true;
//             selfOrderRef = selfOrder || null;
//             state.posConfigId = posConfigId;

//             if (initial && typeof initial === "object") {
//                 if (Array.isArray(initial.availableIds)) {
//                     state.availableIds.splice(0, state.availableIds.length, ...initial.availableIds);
//                 }
//                 if (initial.productSlots) {
//                     Object.assign(state.productSlots, initial.productSlots);
//                 }
//                 if (initial.productMinSlotCode) {
//                     Object.assign(state.productMinSlotCode, initial.productMinSlotCode);
//                 }
//                 state.machineFaultBlocked = Boolean(initial.machineFaultBlocked);
//                 state.machineHasFaultBlockedSlots = Boolean(initial.machineHasFaultBlockedSlots);
//                 state.machineFaultBlockedSlotsCount = Number(initial.machineFaultBlockedSlotsCount || 0);
//             }

//             console.log(
//                 `[vending_product] start posConfigId=${posConfigId} ` +
//                 `initialAvailable=${state.availableIds.length} blocked=${state.machineFaultBlocked}`
//             );

//             // Reconciliar URL con el state inicial — caso F5 en /vending-out-of-service
//             // con máquina ya activa, o F5 en raíz con máquina caída.
//             // SOLO si tenemos initial válido (selfOrder.config). En auto-start
//             // por URL, initial es null y dejamos que la primera respuesta del
//             // poll dispare la reconciliación vía hasReconciledOnce.
//             if (typeof window !== "undefined" && initial) {
//                 const path = window.location.pathname || "";
//                 const isBlockedRoute = HARD_REDIRECT_BLOCKED_ROUTES.some(r => path.endsWith(`/${r}`));
//                 if (!isBlockedRoute) {
//                     const onOOS = path.endsWith("/vending-out-of-service");
//                     const initialDegraded = Boolean(state.machineFaultBlocked) || state.availableIds.length === 0;
//                     if (!initialDegraded && onOOS) {
//                         const target = `/pos-self/${posConfigId}`;
//                         console.log(`[vending_product] HARD REDIRECT (boot reconcile, healthy) -> ${target}`);
//                         hasReconciledOnce = true;
//                         window.location.assign(target);
//                         return;
//                     }
//                     if (initialDegraded && !onOOS) {
//                         const target = `/pos-self/${posConfigId}/vending-out-of-service`;
//                         console.log(`[vending_product] HARD REDIRECT (boot reconcile, degraded) -> ${target}`);
//                         hasReconciledOnce = true;
//                         window.location.assign(target);
//                         return;
//                     }
//                     hasReconciledOnce = true;
//                 }
//             }

//             busChannel = `vending_products_${posConfigId}`;
//             try {
//                 bus_service.addChannel(busChannel);
//                 if (!busListenerAttached) {
//                     bus_service.addEventListener("notification", onBusNotification);
//                     busListenerAttached = true;
//                 }
//             } catch (err) {
//                 console.warn("[vending_product] bus subscribe failed:", err);
//             }

//             if (typeof document !== "undefined" && !visibilityListenerAttached) {
//                 document.addEventListener("visibilitychange", onVisibilityChange);
//                 visibilityListenerAttached = true;
//             }

//             pollNow().finally(() => {
//                 state.ready = true;
//             });
//             _ensurePolling();
//         }

//         const svc = {
//             state,
//             start: startService,
//             reload: pollNow,
//             setBeforeMutate,
//             subscribe,
//         };

//         // Handle global de debug — útil para inspeccionar desde la consola
//         // del kiosko sin abrir el código (p. ej. `window.__vending.state`).
//         if (typeof window !== "undefined") {
//             window.__vending = svc;
//         }

//         // ── AUTO-ARRANQUE INDEPENDIENTE DEL PATCH OWL ──
//         // En producción (sin ?debug=assets) el patch del root selfOrderIndex
//         // NO está enganchando el onMounted, así que start() nunca se llamaba
//         // y el polling no arrancaba. Acá lo arrancamos solos parseando el
//         // URL: si el path matchea /pos-self/<id>, sabemos el config_id y
//         // podemos polear sin depender de OWL.
//         if (typeof window !== "undefined") {
//             const tryAutoStart = () => {
//                 if (started) return;
//                 const path = window.location.pathname || "";
//                 const match = path.match(/\/pos-self\/(\d+)/);
//                 if (!match) return;
//                 const posConfigId = parseInt(match[1], 10);
//                 if (!Number.isFinite(posConfigId)) return;
//                 console.log(`[vending_product] auto-start from URL posConfigId=${posConfigId}`);
//                 try {
//                     startService({ posConfigId, selfOrder: null, initial: null });
//                 } catch (err) {
//                     console.error("[vending_product] auto-start failed:", err);
//                 }
//             };
//             // Disparamos inmediatamente y también en el próximo tick por si
//             // el bundle se evalúa antes de que window.location esté lista.
//             tryAutoStart();
//             setTimeout(tryAutoStart, 0);
//         }

//         return svc;
//     },
// };

// // Wrapper que cachea la instancia del service. Permite que tanto el registry
// // de Odoo (vía useService) como nuestro auto-arranque al boot devuelvan
// // EXACTAMENTE la misma instancia singleton.
// let _vendingSvcInstance = null;
// const vendingProductServiceCached = {
//     dependencies: ["bus_service"],
//     start(env, deps) {
//         if (_vendingSvcInstance) return _vendingSvcInstance;
//         _vendingSvcInstance = vendingProductService.start(env, deps);
//         return _vendingSvcInstance;
//     },
// };
// registry.category("services").add("vending_product", vendingProductServiceCached);
// console.log("[vending_kiosk_ui] vending_product service registered");

// // CRÍTICO: en producción (sin ?debug=assets) el patch del root NO está
// // enganchando, así que NADIE llama a useService("vending_product") y la
// // factory nunca corre — sin polling, el frontend queda muerto.
// // Forzamos la instanciación al boot con un stub bus_service. El polling
// // funciona perfecto sin bus (es solo un acelerador). El URL parsing dentro
// // de la factory levanta el polling con el posConfigId correcto.
// if (typeof window !== "undefined") {
//     setTimeout(() => {
//         if (_vendingSvcInstance) return;
//         console.log("[vending_kiosk_ui] forcing service instantiation at boot");
//         const stubBus = {
//             addChannel: () => {},
//             addEventListener: () => {},
//             deleteChannel: () => {},
//             removeEventListener: () => {},
//         };
//         try {
//             vendingProductServiceCached.start({}, { bus_service: stubBus });
//         } catch (err) {
//             console.error("[vending_kiosk_ui] forced instantiation failed:", err);
//         }
//     }, 0);
// }

// // ════════════════════════════════════════════════════════════════════════════
// // 2) Patch del root selfOrderIndex.
// // ════════════════════════════════════════════════════════════════════════════

// // Rutas del flujo vending donde NO debemos interrumpir con una navegación a
// // out-of-service: el usuario está en medio de un pago o ya llegó al resultado.
// const VENDING_IN_PROGRESS_ROUTES = [
//     "vending-processing",
//     "vending-process",
//     "vending-success",
//     "vending-payment-success",
//     "vending-error",
// ];

// function _isInProgressRoute() {
//     const path = (typeof window !== "undefined" && window.location?.pathname) || "";
//     return VENDING_IN_PROGRESS_ROUTES.some((name) => path.endsWith(`/${name}`));
// }

// function _isOnOutOfService() {
//     const path = (typeof window !== "undefined" && window.location?.pathname) || "";
//     return path.endsWith("/vending-out-of-service");
// }

// export function _buildVendingInitialState(selfOrder) {
//     const cfg = selfOrder?.config || {};
//     return {
//         availableIds: [...(cfg._vending_available_products || [])],
//         productSlots: { ...(cfg._vending_product_slots || {}) },
//         productMinSlotCode: { ...(cfg._vending_product_min_slot_code || {}) },
//         machineFaultBlocked: Boolean(cfg._vending_machine_fault_blocked),
//         machineHasFaultBlockedSlots: Boolean(cfg._vending_machine_has_fault_blocked_slots),
//         machineFaultBlockedSlotsCount: Number(cfg._vending_machine_fault_blocked_slots_count || 0),
//     };
// }

// patch(selfOrderIndex, {
//     components: { ... },
//     setup() { ... },
//     _routeForVendingState(snapshot) { ... },
//     get selfIsReady() { ... },
//     get showAdBanner() { ... },
// });
// FIN DE VERSIÓN ANTERIOR

/** @odoo-module **/

console.log("[vending_kiosk_ui] routes.js module loaded");

import { registry } from "@web/core/registry";
import { reactive, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { selfOrderIndex } from "@pos_self_order/app/self_order_index";
import { useService } from "@web/core/utils/hooks";
import { VendingProcessingScreen } from "./screens/vending_processing_screen/vending_processing_screen";
import { VendingSuccessScreen } from "./screens/vending_success_screen/vending_success_screen";
import { VendingPaymentSuccessScreen } from "./screens/vending_payment_success_screen/vending_payment_success_screen";
import { VendingErrorScreen } from "./screens/vending_error_screen/vending_error_screen";
import { VendingOutOfServiceScreen } from "./screens/vending_out_of_service_screen/vending_out_of_service_screen";

// ════════════════════════════════════════════════════════════════════════════
// 1) Service `vending_product` registrado in-place.
//    Sostiene el polling/bus de productos vending fuera del lifecycle de los
//    componentes, así sobrevive a re-renders del root y al cartel "Hey, looks
//    like..." nativo si llegara a aparecer durante un instante.
// ════════════════════════════════════════════════════════════════════════════

const POLL_FAST_MS = 3000;

export const vendingProductService = {
    dependencies: ["bus_service"],

    start(env, { bus_service }) {
        console.log("[vending_product] service factory start() called");
        const state = reactive({
            ready: false,
            posConfigId: null,
            availableIds: [],
            productSlots: {},
            productMinSlotCode: {},
            productMeta: {},
            machineFaultBlocked: false,
            machineHasFaultBlockedSlots: false,
            machineFaultBlockedSlotsCount: 0,
            lastPollAt: null,
            lastPollOk: false,
        });

        let pollTimer = null;
        let busChannel = null;
        let currentHash = "";
        let started = false;
        let hasReconciledOnce = false; // primera vez que pollNow trae datos del servidor
        let lastKnownRefreshToken = null; // botón "refrescar pantalla del kiosko"
        let busListenerAttached = false;
        let visibilityListenerAttached = false;
        let selfOrderRef = null;

        function _toTemplateId(product) {
            if (!product) return null;
            const maybeId = product.product_tmpl_id?.id ?? product.id;
            const numericId = Number(maybeId);
            return Number.isFinite(numericId) ? numericId : null;
        }

        function _iterProductCandidates(visitor) {
            if (!selfOrderRef) return;
            const visited = new Set();
            const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

            const visit = (product) => {
                if (!product || typeof product !== "object") return;
                const looksLikeProduct = (
                    hasOwn(product, "display_name")
                    || hasOwn(product, "list_price")
                    || hasOwn(product, "public_description")
                    || hasOwn(product, "product_tmpl_id")
                );
                if (!looksLikeProduct) return;
                if (visited.has(product)) return;
                visited.add(product);
                visitor(product);
            };

            const modelContainers = [];
            const models = selfOrderRef?.models;

            if (Array.isArray(models)) {
                modelContainers.push(models);
            } else if (models && typeof models === "object") {
                const productModelKeys = new Set([
                    "product.template",
                    "product_template",
                    "product.product",
                    "product_product",
                ]);
                for (const [key, value] of Object.entries(models)) {
                    if (!productModelKeys.has(key)) continue;
                    if (Array.isArray(value)) {
                        modelContainers.push(value);
                    } else if (value && typeof value === "object") {
                        modelContainers.push(Object.values(value));
                    }
                }
            }

            const extraCollections = [
                selfOrderRef?.products,
                selfOrderRef?.productTemplates,
                selfOrderRef?.productById && Object.values(selfOrderRef.productById),
            ];

            for (const collection of [...modelContainers, ...extraCollections]) {
                if (!collection) continue;
                if (Array.isArray(collection)) {
                    for (const item of collection) {
                        visit(item);
                    }
                }
            }

            visit(selfOrderRef?.selectedVendingProduct);
        }

        function _applyMetaToProduct(product, meta) {
            const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
            const normalizeOdooDateTime = (value) => {
                if (!value || typeof value !== "string") return false;
                const trimmed = value.trim();
                if (!trimmed) return false;
                const normalized = trimmed.replace("T", " ").split(".")[0];
                return normalized || false;
            };
            if (!product || !meta) return;

            if (hasOwn(meta, "display_name")) {
                product.display_name = meta.display_name || "";
                if (hasOwn(product, "name")) {
                    product.name = meta.display_name || product.name || "";
                }
            }
            if (hasOwn(meta, "public_description")) {
                product.public_description = meta.public_description || false;
            }
            if (hasOwn(meta, "write_date")) {
                product.write_date = normalizeOdooDateTime(meta.write_date);
            }
            if (hasOwn(meta, "price")) {
                const parsedPrice = Number(meta.price);
                if (Number.isFinite(parsedPrice)) {
                    product.list_price = parsedPrice;
                    if (hasOwn(product, "lst_price")) {
                        product.lst_price = parsedPrice;
                    }
                    product._vending_price_override = parsedPrice;
                }
            }
        }

        function applyProductMeta(productMeta) {
            if (!productMeta || typeof productMeta !== "object") return;

            for (const key of Object.keys(state.productMeta)) {
                delete state.productMeta[key];
            }
            Object.assign(state.productMeta, productMeta);

            _iterProductCandidates((product) => {
                const templateId = _toTemplateId(product);
                if (!templateId) return;
                const meta = productMeta[templateId] || productMeta[String(templateId)];
                if (!meta) return;
                _applyMetaToProduct(product, meta);
            });

            const selected = selfOrderRef?.selectedVendingProduct;
            const selectedTemplateId = _toTemplateId(selected);
            if (selectedTemplateId) {
                const selectedMeta = productMeta[selectedTemplateId] || productMeta[String(selectedTemplateId)];
                if (selectedMeta) {
                    _applyMetaToProduct(selected, selectedMeta);
                }
            }
        }

        let beforeMutateHook = null;

        function setBeforeMutate(hook) {
            beforeMutateHook = typeof hook === "function" ? hook : null;
        }

        // Suscripción independiente del ciclo de render OWL — el root no
        // re-renderiza ante cambios de vendingState (su template no lee el
        // proxy reactivo), así que useEffect no dispararía. Acá llamamos a
        // los suscriptores DESPUÉS de cada updateProducts, sea quien sea.
        const subscribers = new Set();
        function subscribe(cb) {
            if (typeof cb !== "function") return () => {};
            subscribers.add(cb);
            return () => subscribers.delete(cb);
        }
        function notifySubscribers() {
            for (const cb of subscribers) {
                try { cb(); } catch (err) {
                    console.warn("[vending_product] subscriber error:", err);
                }
            }
        }

        // Rutas en las que NUNCA forzamos un reload (pago en curso).
        const HARD_REDIRECT_BLOCKED_ROUTES = [
            "vending-processing",
            "vending-process",
            "vending-success",
            "vending-payment-success",
            "vending-error",
        ];

        function _hardRedirectIfNeeded(prevDegraded, prevAvailable, nextDegraded, nextAvailable) {
            if (typeof window === "undefined" || !state.posConfigId) return;
            const path = window.location.pathname || "";
            const isBlockedRoute = HARD_REDIRECT_BLOCKED_ROUTES.some(r => path.endsWith(`/${r}`));
            if (isBlockedRoute) return;

            const onOOS = path.endsWith("/vending-out-of-service");
            const transitionedToHealthy = prevDegraded && !nextDegraded;
            const transitionedToDegraded = !prevDegraded && nextDegraded;

            // Primera vez que tenemos datos del servidor: reconciliar URL
            // incondicionalmente (sin importar transición), porque venimos de
            // un default ambiguo (vacío) que no refleja el estado real.
            const firstReconcile = !hasReconciledOnce;
            hasReconciledOnce = true;

            if ((transitionedToHealthy || firstReconcile) && !nextDegraded && onOOS) {
                const target = `/pos-self/${state.posConfigId}`;
                console.log(`[vending_product] HARD REDIRECT (healthy) -> ${target}`);
                window.location.assign(target);
                return;
            }
            if ((transitionedToDegraded || firstReconcile) && nextDegraded && !onOOS) {
                const target = `/pos-self/${state.posConfigId}/vending-out-of-service`;
                console.log(`[vending_product] HARD REDIRECT (degraded) -> ${target}`);
                window.location.assign(target);
            }
        }

        function updateProducts(newIds, newSlots, newProductMinSlotCode, newProductMeta, machineState = null) {
            // Snapshot del state PREVIO para detectar transiciones blocked↔healthy.
            const prevDegraded = Boolean(state.machineFaultBlocked) || state.availableIds.length === 0;
            const prevAvailableLen = state.availableIds.length;

            // Hook sincrónico ANTES de tocar el state reactivo. routes.js lo
            // usa para navegar fuera de /products cuando el catálogo se va a
            // vaciar — así ProductListPage no llega a re-renderizar con lista
            // vacía y se evita el crash de category_scrollspy_hook.
            if (beforeMutateHook) {
                try {
                    beforeMutateHook({
                        nextAvailableIds: newIds || [],
                        nextMachineFaultBlocked: !!(machineState && machineState.machineFaultBlocked),
                    });
                } catch (err) {
                    console.warn("[vending_product] beforeMutate hook failed:", err);
                }
            }
            state.availableIds.splice(0, state.availableIds.length, ...newIds);
            if (newSlots) {
                for (const key of Object.keys(state.productSlots)) {
                    delete state.productSlots[key];
                }
                Object.assign(state.productSlots, newSlots);
            }
            if (newProductMinSlotCode) {
                for (const key of Object.keys(state.productMinSlotCode)) {
                    delete state.productMinSlotCode[key];
                }
                Object.assign(state.productMinSlotCode, newProductMinSlotCode);
            }
            if (newProductMeta) {
                applyProductMeta(newProductMeta);
            }
            if (machineState && typeof machineState === "object") {
                state.machineFaultBlocked = Boolean(machineState.machineFaultBlocked);
                state.machineHasFaultBlockedSlots = Boolean(machineState.machineHasFaultBlockedSlots);
                state.machineFaultBlockedSlotsCount = Number(machineState.machineFaultBlockedSlotsCount || 0);
            }
            notifySubscribers();

            // Fallback bruto: si por cualquier razón los hooks/subscribe no
            // navegan (race de patch, useState wrapper, lo que sea), forzamos
            // un window.location.assign para garantizar que el frontend
            // refleje el cambio sin reload manual.
            const nextDegraded = Boolean(state.machineFaultBlocked) || state.availableIds.length === 0;
            _hardRedirectIfNeeded(prevDegraded, prevAvailableLen, nextDegraded, state.availableIds.length);
        }

        async function pollNow() {
            if (!state.posConfigId) return;
            console.log(`[vending_product] pollNow tick (configId=${state.posConfigId})`);
            try {
                const resp = await rpc("/v1/vending/products/poll", {
                    pos_config_id: state.posConfigId,
                    current_hash: currentHash,
                });
                state.lastPollAt = Date.now();

                if (resp.error) {
                    state.lastPollOk = false;
                    console.warn("[vending_product] poll error from server:", resp.error);
                    return;
                }

                state.lastPollOk = true;
                currentHash = resp.hash || "";

                // Detectar pulsación del botón "Refrescar pantalla del kiosko"
                // del form de la máquina: el backend incrementa kiosk_refresh_token,
                // entra en el hash y nos llega acá. Forzamos un reload duro a la raíz.
                const incomingToken = Number.isFinite(resp.kiosk_refresh_token)
                    ? Number(resp.kiosk_refresh_token)
                    : null;
                if (incomingToken !== null) {
                    if (lastKnownRefreshToken === null) {
                        // Primera vez que vemos el token: lo registramos sin redirect.
                        lastKnownRefreshToken = incomingToken;
                    } else if (incomingToken > lastKnownRefreshToken) {
                        const path = window.location.pathname || "";
                        const isBlockedRoute = HARD_REDIRECT_BLOCKED_ROUTES.some(r => path.endsWith(`/${r}`));
                        if (!isBlockedRoute) {
                            const target = `/pos-self/${state.posConfigId}`;
                            console.log(
                                `[vending_product] HARD REDIRECT (manual refresh button, token ${lastKnownRefreshToken}->${incomingToken}) -> ${target}`
                            );
                            lastKnownRefreshToken = incomingToken;
                            window.location.assign(target);
                            return;
                        }
                        lastKnownRefreshToken = incomingToken;
                    }
                }

                if (!resp.changed) return;

                console.log(
                    `[vending_product] poll change ids=${(resp.product_ids || []).length} ` +
                    `blocked=${!!resp.machine_fault_blocked}`
                );

                updateProducts(
                    resp.product_ids || [],
                    resp.product_slots || null,
                    resp.product_min_slot_code || null,
                    resp.product_meta || null,
                    {
                        machineFaultBlocked: resp.machine_fault_blocked,
                        machineHasFaultBlockedSlots: resp.machine_has_fault_blocked_slots,
                        machineFaultBlockedSlotsCount: resp.machine_fault_blocked_slots_count,
                    },
                );
            } catch (err) {
                state.lastPollOk = false;
                console.warn("[vending_product] poll network error:", err);
            }
        }

        // Polling robusto basado en setInterval fijo a la cadencia rápida
        // (3s). El endpoint /poll devuelve `changed: false` rapidísimo cuando
        // no hay cambios (compara hash); no necesitamos cadencia adaptativa.
        // Un solo timer global imposible de "olvidar" como pasaba con la
        // cadena de setTimeout recursivos.
        function _stopPolling() {
            if (pollTimer) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
        }

        function _ensurePolling() {
            if (pollTimer) return;
            console.log(`[vending_product] interval armed every ${POLL_FAST_MS}ms`);
            pollTimer = setInterval(() => {
                pollNow();
            }, POLL_FAST_MS);
        }

        function onBusNotification({ detail: notifications }) {
            if (!notifications || !Array.isArray(notifications) || !busChannel) return;
            const relevant = notifications.filter(n => n.payload?.channel === busChannel);
            if (!relevant.length) return;
            for (const notif of relevant) {
                const msg = notif.payload;
                if (msg.type !== "vending_products_update") continue;
                console.log(
                    `[vending_product] bus notif ids=${(msg.all_available_ids || []).length} ` +
                    `blocked=${!!msg.machine_fault_blocked}`
                );
                updateProducts(msg.all_available_ids || [], null, null, null, {
                    machineFaultBlocked: msg.machine_fault_blocked,
                    machineHasFaultBlockedSlots: msg.machine_has_fault_blocked_slots,
                    machineFaultBlockedSlotsCount: msg.machine_fault_blocked_slots_count,
                });
                pollNow();
            }
        }

        function onVisibilityChange() {
            if (typeof document === "undefined" || document.visibilityState !== "visible") return;
            if (!started) return;
            console.log("[vending_product] visibility -> visible, re-polling");
            pollNow();
            _ensurePolling();
        }

        function startService({ posConfigId, selfOrder, initial }) {
            if (started) return;
            if (!posConfigId) {
                console.warn("[vending_product] start aborted: no posConfigId");
                return;
            }
            started = true;
            selfOrderRef = selfOrder || null;
            state.posConfigId = posConfigId;

            if (initial && typeof initial === "object") {
                if (Array.isArray(initial.availableIds)) {
                    state.availableIds.splice(0, state.availableIds.length, ...initial.availableIds);
                }
                if (initial.productSlots) {
                    Object.assign(state.productSlots, initial.productSlots);
                }
                if (initial.productMinSlotCode) {
                    Object.assign(state.productMinSlotCode, initial.productMinSlotCode);
                }
                state.machineFaultBlocked = Boolean(initial.machineFaultBlocked);
                state.machineHasFaultBlockedSlots = Boolean(initial.machineHasFaultBlockedSlots);
                state.machineFaultBlockedSlotsCount = Number(initial.machineFaultBlockedSlotsCount || 0);
            }

            console.log(
                `[vending_product] start posConfigId=${posConfigId} ` +
                `initialAvailable=${state.availableIds.length} blocked=${state.machineFaultBlocked}`
            );

            // Reconciliar URL con el state inicial — caso F5 en /vending-out-of-service
            // con máquina ya activa, o F5 en raíz con máquina caída.
            // SOLO si tenemos initial válido (selfOrder.config). En auto-start
            // por URL, initial es null y dejamos que la primera respuesta del
            // poll dispare la reconciliación vía hasReconciledOnce.
            if (typeof window !== "undefined" && initial) {
                const path = window.location.pathname || "";
                const isBlockedRoute = HARD_REDIRECT_BLOCKED_ROUTES.some(r => path.endsWith(`/${r}`));
                if (!isBlockedRoute) {
                    const onOOS = path.endsWith("/vending-out-of-service");
                    const initialDegraded = Boolean(state.machineFaultBlocked) || state.availableIds.length === 0;
                    if (!initialDegraded && onOOS) {
                        const target = `/pos-self/${posConfigId}`;
                        console.log(`[vending_product] HARD REDIRECT (boot reconcile, healthy) -> ${target}`);
                        hasReconciledOnce = true;
                        window.location.assign(target);
                        return;
                    }
                    if (initialDegraded && !onOOS) {
                        const target = `/pos-self/${posConfigId}/vending-out-of-service`;
                        console.log(`[vending_product] HARD REDIRECT (boot reconcile, degraded) -> ${target}`);
                        hasReconciledOnce = true;
                        window.location.assign(target);
                        return;
                    }
                    hasReconciledOnce = true;
                }
            }

            busChannel = `vending_products_${posConfigId}`;
            try {
                bus_service.addChannel(busChannel);
                if (!busListenerAttached) {
                    bus_service.addEventListener("notification", onBusNotification);
                    busListenerAttached = true;
                }
            } catch (err) {
                console.warn("[vending_product] bus subscribe failed:", err);
            }

            if (typeof document !== "undefined" && !visibilityListenerAttached) {
                document.addEventListener("visibilitychange", onVisibilityChange);
                visibilityListenerAttached = true;
            }

            pollNow().finally(() => {
                state.ready = true;
            });
            _ensurePolling();
        }

        const svc = {
            state,
            start: startService,
            reload: pollNow,
            setBeforeMutate,
            subscribe,
        };

        // Handle global de debug — útil para inspeccionar desde la consola
        // del kiosko sin abrir el código (p. ej. `window.__vending.state`).
        if (typeof window !== "undefined") {
            window.__vending = svc;
        }

        // ── AUTO-ARRANQUE INDEPENDIENTE DEL PATCH OWL ──
        // En producción (sin ?debug=assets) el patch del root selfOrderIndex
        // NO está enganchando el onMounted, así que start() nunca se llamaba
        // y el polling no arrancaba. Acá lo arrancamos solos parseando el
        // URL: si el path matchea /pos-self/<id>, sabemos el config_id y
        // podemos polear sin depender de OWL.
        if (typeof window !== "undefined") {
            const tryAutoStart = () => {
                if (started) return;
                const path = window.location.pathname || "";
                const match = path.match(/\/pos-self\/(\d+)/);
                if (!match) return;
                const posConfigId = parseInt(match[1], 10);
                if (!Number.isFinite(posConfigId)) return;
                console.log(`[vending_product] auto-start from URL posConfigId=${posConfigId}`);
                try {
                    startService({ posConfigId, selfOrder: null, initial: null });
                } catch (err) {
                    console.error("[vending_product] auto-start failed:", err);
                }
            };
            // Disparamos inmediatamente y también en el próximo tick por si
            // el bundle se evalúa antes de que window.location esté lista.
            tryAutoStart();
            setTimeout(tryAutoStart, 0);
        }

        return svc;
    },
};

// Wrapper que cachea la instancia del service. Permite que tanto el registry
// de Odoo (vía useService) como nuestro auto-arranque al boot devuelvan
// EXACTAMENTE la misma instancia singleton.
let _vendingSvcInstance = null;
const vendingProductServiceCached = {
    dependencies: ["bus_service"],
    start(env, deps) {
        if (_vendingSvcInstance) return _vendingSvcInstance;
        _vendingSvcInstance = vendingProductService.start(env, deps);
        return _vendingSvcInstance;
    },
};
registry.category("services").add("vending_product", vendingProductServiceCached);
console.log("[vending_kiosk_ui] vending_product service registered");

// CRÍTICO: en producción (sin ?debug=assets) el patch del root NO está
// enganchando, así que NADIE llama a useService("vending_product") y la
// factory nunca corre — sin polling, el frontend queda muerto.
// Forzamos la instanciación al boot con un stub bus_service. El polling
// funciona perfecto sin bus (es solo un acelerador). El URL parsing dentro
// de la factory levanta el polling con el posConfigId correcto.
if (typeof window !== "undefined") {
    setTimeout(() => {
        if (_vendingSvcInstance) return;
        console.log("[vending_kiosk_ui] forcing service instantiation at boot");
        const stubBus = {
            addChannel: () => {},
            addEventListener: () => {},
            deleteChannel: () => {},
            removeEventListener: () => {},
        };
        try {
            vendingProductServiceCached.start({}, { bus_service: stubBus });
        } catch (err) {
            console.error("[vending_kiosk_ui] forced instantiation failed:", err);
        }
    }, 0);
}

// ════════════════════════════════════════════════════════════════════════════
// 2) Extensión del root selfOrderIndex.
//
// FIX CRÍTICO (junio 2026): patch() de Odoo no funciona acá porque root.js
// captura la referencia a selfOrderIndex con `import { selfOrderIndex as Index }`
// y llama a mountComponent(Index, ...) ANTES de que nuestro archivo corra.
// El patch se aplica sobre la clase correcta pero Index ya apunta a la
// referencia vieja, así que el componente montado nunca tiene nuestros métodos.
//
// SOLUCIÓN: asignar directamente al PROTOTIPO de la clase. Esto garantiza que
// sin importar el orden de carga, los métodos queden en la cadena prototípica
// que el componente montado ya usa. No usamos patch() de Odoo acá.
// ════════════════════════════════════════════════════════════════════════════

// Rutas del flujo vending donde NO debemos interrumpir con una navegación a
// out-of-service: el usuario está en medio de un pago o ya llegó al resultado.
const VENDING_IN_PROGRESS_ROUTES = [
    "vending-processing",
    "vending-process",
    "vending-success",
    "vending-payment-success",
    "vending-error",
];

function _isInProgressRoute() {
    const path = (typeof window !== "undefined" && window.location?.pathname) || "";
    return VENDING_IN_PROGRESS_ROUTES.some((name) => path.endsWith(`/${name}`));
}

function _isOnOutOfService() {
    const path = (typeof window !== "undefined" && window.location?.pathname) || "";
    return path.endsWith("/vending-out-of-service");
}

export function _buildVendingInitialState(selfOrder) {
    const cfg = selfOrder?.config || {};
    return {
        availableIds: [...(cfg._vending_available_products || [])],
        productSlots: { ...(cfg._vending_product_slots || {}) },
        productMinSlotCode: { ...(cfg._vending_product_min_slot_code || {}) },
        machineFaultBlocked: Boolean(cfg._vending_machine_fault_blocked),
        machineHasFaultBlockedSlots: Boolean(cfg._vending_machine_has_fault_blocked_slots),
        machineFaultBlockedSlotsCount: Number(cfg._vending_machine_fault_blocked_slots_count || 0),
    };
}

// ── Componentes adicionales (pantallas vending) ──
// Se agregan al static components de la clase para que el Router los pueda
// resolver cuando navega a las rutas /vending-*.
selfOrderIndex.components = {
    ...selfOrderIndex.components,
    VendingProcessingScreen,
    VendingSuccessScreen,
    VendingPaymentSuccessScreen,
    VendingErrorScreen,
    VendingOutOfServiceScreen,
};

// ── setup() parcheado ──
// Guardamos el setup original y lo llamamos con super equivalente.
// CRÍTICO: registramos los hooks de OWL INCONDICIONALMENTE.
// El check `self_ordering_mode === "vending"` se hace DENTRO de los
// callbacks, no acá. selfOrder.config puede estar undefined durante
// el primer ciclo síncrono de setup (la promise de SelfOrder.ready
// no resolvió aún), y un early-return acá perdería los hooks.
const _originalSetup = selfOrderIndex.prototype.setup;
selfOrderIndex.prototype.setup = function () {
    _originalSetup.call(this);
    this.router = useService("router");

    // ── Banner publicitario (vending) ──
    // Estado reactivo que sigue la ruta actual para decidir si mostramos
    // el video de publicidad. El video se muestra en lista de productos y
    // fuera de servicio, y se OCULTA durante el flujo de pago.
    // Usamos polling de window.location.pathname (200ms) porque el Router
    // de Odoo no expone un estado reactivo de ruta accesible desde acá.
    this.adState = useState({ pathname: (typeof window !== "undefined" && window.location?.pathname) || "" });
    const _adRouteTimer = setInterval(() => {
        const p = (typeof window !== "undefined" && window.location?.pathname) || "";
        if (p !== this.adState.pathname) {
            this.adState.pathname = p;
        }
    }, 200);
    onWillUnmount(() => clearInterval(_adRouteTimer));

    let svc;
    try {
        svc = useService("vending_product");
    } catch (err) {
        console.error("[vending_route] useService(vending_product) failed:", err);
        return;
    }
    this.vendingProductService = svc;

    // Helper que construye un snapshot fresco leyendo DIRECTO del proxy
    // reactivo del service (no a través de un wrapper de useState).
    // No usamos useState(svc.state) — introducía un wrapper que no
    // compartía observadores con el reactive original, devolviendo valores stale.
    const buildSnapshot = () => ({
        machineFaultBlocked: Boolean(svc.state.machineFaultBlocked),
        availableIds: [...svc.state.availableIds],
    });

    // Suscripción al service — se llama desde updateProducts() del
    // service después de cada mutación. Construimos snapshot fresco y
    // se lo pasamos a _routeForVendingState (mismo contrato que el OLD).
    const unsubscribe = svc.subscribe(() => {
        this._routeForVendingState(buildSnapshot());
    });
    onWillUnmount(() => { unsubscribe(); });

    // Hook sync ANTES de cada mutación de productos. Si el cambio nos
    // lleva a una pantalla degradada (sin catálogo o con la máquina
    // bloqueada) y estamos en una ruta que renderiza productos,
    // navegamos ANTES — así ProductListPage se desmonta limpio y el
    // category_scrollspy nativo no llega a leer un ref nulo.
    svc.setBeforeMutate(({ nextAvailableIds, nextMachineFaultBlocked }) => {
        if (this.selfOrder?.config?.self_ordering_mode !== "vending") return;
        const goingDegraded = nextMachineFaultBlocked || !nextAvailableIds.length;
        if (!goingDegraded) return;
        if (_isInProgressRoute() || _isOnOutOfService()) return;
        const posConfigId = this.selfOrder?.config?.id;
        if (!posConfigId) return;
        console.log(
            "[vending_route] preemptive navigate -> vending-out-of-service " +
            `(blocked=${nextMachineFaultBlocked}, products=${nextAvailableIds.length})`
        );
        this.router.navigate(`/pos-self/${posConfigId}/vending-out-of-service`);
    });

    // Arrancar polling y evaluar navegación inicial UNA VEZ que el
    // componente está montado (selfOrder.config ya garantizado poblado).
    onMounted(() => {
        if (this.selfOrder?.config?.self_ordering_mode !== "vending") {
            console.log("[vending_route] onMounted — not vending mode, skipping");
            return;
        }
        console.log("[vending_route] onMounted — starting service", {
            configId: this.selfOrder.config.id,
        });
        try {
            svc.start({
                posConfigId: this.selfOrder.config.id,
                selfOrder: this.selfOrder,
                initial: _buildVendingInitialState(this.selfOrder),
            });
        } catch (err) {
            console.error("[vending_route] service.start failed:", err);
        }
        // Después de start(), state ya tiene la hidratación inicial.
        this._routeForVendingState(buildSnapshot());
    });
};

// ── _routeForVendingState ──
// Evalúa el estado de la máquina y navega a la pantalla correcta.
// Se llama desde el subscriber (cada poll) y desde onMounted (arranque).
selfOrderIndex.prototype._routeForVendingState = function (snapshot) {
    if (this.selfOrder?.config?.self_ordering_mode !== "vending") return;
    const posConfigId = this.selfOrder?.config?.id;
    if (!posConfigId) return;

    const machineBlocked = Boolean(snapshot?.machineFaultBlocked);
    const availableIds = snapshot?.availableIds || [];
    const hasProducts = Array.isArray(availableIds) && availableIds.length > 0;
    const shouldShowOutOfService = machineBlocked || !hasProducts;

    if (shouldShowOutOfService) {
        if (_isInProgressRoute() || _isOnOutOfService()) return;
        console.log(
            `[vending_route] navigate -> vending-out-of-service ` +
            `(blocked=${machineBlocked}, products=${availableIds.length})`
        );
        this.router.navigate(`/pos-self/${posConfigId}/vending-out-of-service`);
        return;
    }

    if (_isOnOutOfService()) {
        console.log(
            `[vending_route] navigate -> default ` +
            `(blocked=${machineBlocked}, products=${availableIds.length})`
        );
        this.router.navigate("default");
    }
};

// ── selfIsReady ──
// En modo vending el Router siempre debe cargar; la pantalla de fuera
// de servicio se muestra navegando a /vending-out-of-service desde
// _routeForVendingState. Defensa secundaria: el inherit XML reemplaza el
// t-else nativo por VendingOutOfServiceScreen en modo vending.
Object.defineProperty(selfOrderIndex.prototype, "selfIsReady", {
    get() {
        if (this.selfOrder?.config?.self_ordering_mode === "vending") {
            return true;
        }
        return this.selfOrder.models["product.product"].length > 0;
    },
    configurable: true,
});

// ── showAdBanner ──
// Decide si mostrar el banner de publicidad (video) sobre el kiosko.
// Solo en modo vending. Se OCULTA durante el flujo de pago
// (vending-process, vending-success, vending-payment-success,
// vending-error) para que el QR ocupe toda la pantalla. Se muestra en
// el resto (lista de productos, fuera de servicio, landing).
Object.defineProperty(selfOrderIndex.prototype, "showAdBanner", {
    get() {
        if (this.selfOrder?.config?.self_ordering_mode !== "vending") {
            return false;
        }
        const path = this.adState?.pathname || "";
        const isPaymentFlow = VENDING_IN_PROGRESS_ROUTES.some(
            (name) => path.endsWith(`/${name}`)
        );
        return !isPaymentFlow;
    },
    configurable: true,
});

console.log("[vending_route] prototipo de selfOrderIndex extendido correctamente");