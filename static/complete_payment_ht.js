/* Hosted Tokenization message helpers. Card fields never exist here. */
(function (root) {
  var SUCCESS_CODE = "001";

  function parseMessageData(raw) {
    if (raw !== null && typeof raw === "object") {
      return raw;
    }
    if (typeof raw === "string") {
      try {
        return JSON.parse(raw);
      } catch (ignore) {
        return null;
      }
    }
    return null;
  }

  function normalizeCodes(responseCode) {
    var values;
    if (Array.isArray(responseCode)) {
      values = responseCode;
    } else if (responseCode === undefined || responseCode === null) {
      values = [];
    } else {
      values = [responseCode];
    }
    var codes = [];
    var i;
    for (i = 0; i < values.length; i += 1) {
      var code = String(values[i]).trim();
      if (/^[0-9]{3}$/.test(code)) {
        codes.push(code);
      }
    }
    return codes;
  }

  function isSuccess(codes) {
    return codes.length === 1 && codes[0] === SUCCESS_CODE;
  }

  function originAllowed(eventOrigin, expectedOrigin) {
    return eventOrigin === expectedOrigin;
  }

  function sourceAllowed(eventSource, expectedSource) {
    return !!expectedSource && eventSource === expectedSource;
  }

  function acceptMessage(event, expectedOrigin, expectedSource) {
    if (!event || !originAllowed(event.origin, expectedOrigin)) {
      return null;
    }
    if (!sourceAllowed(event.source, expectedSource)) {
      return null;
    }
    return parseMessageData(event.data);
  }

  function extractDataKey(payload, minLength, maxLength) {
    if (payload === null || typeof payload !== "object") {
      return null;
    }
    var codes = normalizeCodes(payload.responseCode);
    if (!isSuccess(codes)) {
      return null;
    }
    if (typeof payload.dataKey !== "string") {
      return null;
    }
    var token = payload.dataKey.trim();
    if (token.length < minLength || token.length > maxLength) {
      return null;
    }
    return token;
  }

  function safeFailureStatus(payload) {
    var codes = normalizeCodes(payload && payload.responseCode);
    if (codes.length === 0) {
      return "Hosted Tokenization failed";
    }
    return "Hosted Tokenization failed (code " + codes.join(", ") + ")";
  }

  var api = {
    parseMessageData: parseMessageData,
    normalizeCodes: normalizeCodes,
    isSuccess: isSuccess,
    originAllowed: originAllowed,
    sourceAllowed: sourceAllowed,
    acceptMessage: acceptMessage,
    extractDataKey: extractDataKey,
    safeFailureStatus: safeFailureStatus
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.GmlHostedTokenization = api;
  }
})(typeof window !== "undefined" ? window : this);
