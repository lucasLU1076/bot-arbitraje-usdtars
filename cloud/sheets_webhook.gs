// Web App de Apps Script que recibe las ordenes del bot y las escribe en el
// Google Sheet "Bot Arbitraje USDT-ARS - Registro de Ordenes".
//
// COMO INSTALARLO (una sola vez):
//   1. Abrir el Google Sheet.
//   2. Menu Extensiones > Apps Script.
//   3. Borrar lo que haya en Code.gs y pegar este archivo completo.
//   4. Arriba a la derecha: Implementar > Nueva implementacion.
//   5. Tipo: "Aplicacion web". Ejecutar como: "Yo". Quien tiene acceso: "Cualquier usuario".
//   6. Implementar. Google va a pedir autorizar el script (es tu propia cuenta autorizando
//      tu propio script, no comparte nada con nadie mas).
//   7. Copiar la URL que te da ("URL de la aplicacion web") y pasarsela a Claude.
//
// IMPORTANTE: reemplazar por el valor real de SHEETS_WEBHOOK_TOKEN (ver .env,
// que esta en .gitignore y nunca se sube al repo). Este archivo es publico.
var SECRET_TOKEN = "REEMPLAZAR_CON_EL_TOKEN_DE_.ENV";

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    if (body.token !== SECRET_TOKEN) {
      return jsonOut({ ok: false, error: "token invalido" });
    }

    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
    var row = body.row;

    if (body.action === "append") {
      sheet.appendRow(row);
      return jsonOut({ ok: true, row_number: sheet.getLastRow() });
    }

    if (body.action === "update") {
      var rowNumber = body.row_number;
      sheet.getRange(rowNumber, 1, 1, row.length).setValues([row]);
      return jsonOut({ ok: true, row_number: rowNumber });
    }

    return jsonOut({ ok: false, error: "accion desconocida: " + body.action });
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
}

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
