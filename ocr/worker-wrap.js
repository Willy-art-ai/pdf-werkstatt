/* PDF-Werkstatt: Sprachdaten liegen als .bin vor (manche Server liefern keine .gz-Dateien aus) */
const _pwFetch = self.fetch.bind(self);
self.fetch = (u, o) => _pwFetch(String(u).replace(/\.traineddata\.gz$/, '.traineddata.wasm'), o);
importScripts('worker.min.js');
