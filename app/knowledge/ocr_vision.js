// macOS Vision OCR (Arabic + English). Run: osascript -l JavaScript ocr_vision.js <image>
ObjC.import('Foundation');
ObjC.import('Vision');
ObjC.import('AppKit');

function run(argv) {
  const url = $.NSURL.fileURLWithPath(argv[0]);
  const request = $.VNRecognizeTextRequest.alloc.init;
  request.recognitionLevel = 0; // accurate
  request.usesLanguageCorrection = true;
  request.recognitionLanguages = $(["ar-SA", "en-US"]);
  const handler = $.VNImageRequestHandler.alloc.initWithURLOptions(url, $({}));
  const error = $();
  if (!handler.performRequestsError($([request]), error)) {
    return "ERROR";
  }
  const results = request.results;
  const lines = [];
  for (let i = 0; i < results.count; i++) {
    const candidates = results.objectAtIndex(i).topCandidates(1);
    if (candidates.count > 0) lines.push(candidates.objectAtIndex(0).string.js);
  }
  return lines.join("\n");
}
