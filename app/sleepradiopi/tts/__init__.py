"""On-device text-to-speech via sherpa-onnx's Python binding.

This is the one part of SleepRadioPi that reuses SleepRadio's assets
directly and unchanged: a voice pack exported from the Android app
(model.onnx + tokens.txt + espeak-ng-data/) drops into voices/<id>/ here and
loads with the same sherpa-onnx OfflineTts API, just from Python instead of
the JVM binding.
"""
