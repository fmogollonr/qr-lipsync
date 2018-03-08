#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os

# We don't want to use hw accel since it seems to be messing with latency
os.environ['LIBVA_DRIVER_NAME'] = 'fakedriver'

import re
import shutil
import sys
import time
import subprocess
import logging
import json
from fractions import Fraction
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

import easyevent
easyevent.dispatcher = 'gobject'
from gstmanager import PipelineManager

logger = logging.getLogger('timing_analyzer')


class QrLipsyncDetector(easyevent.User):
    '''
        Open video file with gstreamer.
        Get all QrCode and spectrum events.
        Filter spectrum events by frequency and peak.
        Write in a json file all QrCode events and spectrum events that have an high peak
    '''
    def __init__(self, media_file, result_file, mainloop):
        easyevent.User.__init__(self)
        self.register_event("eos", "barcode", "spectrum")
        self.media_info = self.get_media_info(media_file)
        self._samplerate = int(self.media_info.get('sample_rate', 0))
        self._media_duration = float(self.media_info['duration'])
        self.mainloop = mainloop
        self._media_file = media_file
        self._result_filename = result_file
        self._result_file = open(result_file, 'w')
        self._bands = 1024
        self._check_freq = 0
        self._audio_timestamp = -1
        self._audio_timestamp_saved = -1
        self._magnitude_position = -1
        self._max_magnitude = 0
        self._counter = 0
        self._qrcode_tickfreq_count = 0
        self._tick_count = 0
        self._interval = 3000000
        self._start_time = 0
        self._end_time = 0
        self._json_length = 70
        self._threshold_db = -48.0
        self._min_freq = 400
        self._audio_duration = 0
        self._video_duration = 0

        self._audio_fakesink_pad = None
        self._video_fakesink_pad = None
        self._id_prob_audio_sink = None
        self._id_prob_video_sink = None

        self.size = 0

        self.pipeline_str = self.get_pipeline(self._media_file)
        logger.info(self.pipeline_str)
        self.pipeline = PipelineManager(self.pipeline_str)

    def exit(self):
        self.pipeline.stop()
        self.mainloop.quit()

    def get_pipeline(self, media_file):
        src = 'filesrc location=%s' % (media_file)
        demux = "decodebin name=dec"
        video_width, video_height = self.media_info['width'], self.media_info['height']
        ratio = float(video_width) / float(video_height)
        downscaled_width = 640
        downscaling_height = int(float(downscaled_width) / float(ratio))
        video_downscale_caps = "video/x-raw, format=(string)I420, width=(int)%s, height=(int)%s" % (downscaled_width, downscaling_height)
        qrcode_extract = "zbar name=qroverlay"
        audio_sink = "fakesink silent=false name=afakesink"
        spectrum = "spectrum bands=%s name=spectrum interval=%s" % (self._bands, self._interval)
        progress = "progressreport update-freq=1"
        video_sink = "fakesink silent=false name=vfakesink"
        if self._samplerate:
            return "{src} ! {demux} ! videoscale ! videoconvert ! {video_downscale_caps} ! {qrcode_extract} ! {progress} ! {video_sink} dec. ! queue name=audiodec ! audioconvert ! {spectrum} ! {audio_sink}".format(**locals())
        else:
            return "{src} ! {demux} ! videoscale ! videoconvert ! {video_downscale_caps} ! {qrcode_extract} ! {progress} ! {video_sink}".format(**locals())

    def start(self):
        if not hasattr(self.pipeline, 'pipeline'):
            logger.error('Pipeline could not be parsed, exiting')
            self.exit()
        if self._samplerate:
            audio_fakesink = self.pipeline.pipeline.get_by_name("afakesink")
            self._audio_fakesink_pad = audio_fakesink.get_static_pad('sink')
            self._id_prob_audio_sink = self._audio_fakesink_pad.add_probe(Gst.PadProbeType.BUFFER, self.on_audio_fakesink_buffer, None)
        video_fakesink = self.pipeline.pipeline.get_by_name("vfakesink")
        self._video_src_pad = video_fakesink.get_static_pad('sink')
        self._id_prob_video_sink = self._video_src_pad.add_probe(Gst.PadProbeType.BUFFER, self.on_video_fakesink_buffer, None)

        self.pipeline_success = False
        self.pipeline.run()
        self._start_time = time.time()
        logger.info("start  pipeline")

    def on_audio_fakesink_buffer(self, pad, info, data):
        buf = info.get_buffer()
        self._audio_duration = buf.pts
        return True

    def on_video_fakesink_buffer(self, pad, info, data):
        buf = info.get_buffer()
        self._video_duration = buf.pts
        return True

    def evt_eos(self, event):
        logger.info("eos received, found %s qrcodes and %s ticks" % (self._qrcode_tickfreq_count, self._tick_count))
        self.unregister_event("sos", "eos", "barcode", "spectrum")
        # FIXME disconnect it before eos is applied in pipeline
        # self._disconnect_probes()
        self._end_time = time.time()
        processing_duration = self._end_time - self._start_time
        fps = Fraction(self.media_info['avg_frame_rate']) * self._media_duration / processing_duration
        logger.info("Processing took %.2fs (%i fps)" % (processing_duration, fps))
        duration_string = '{"AUDIODURATION":%s,"VIDEODURATION":%s}' % (self._audio_duration, self._video_duration)
        self.write_line(duration_string)
        self._result_file.close()
        logger.info('Wrote file %s' % self._result_filename)
        self.exit()

    def evt_barcode(self, event):
        elt_name = event.content['source']
        struct = event.content['data']
        timestamp = struct.get_value('timestamp')
        json_data = struct.get_value('symbol')
        #FIXME: qroverlay appends a trailing comma which makes the json invalid {"TIMESTAMP":33333333,"BUFFERCOUNT":2,"FRAMERATE":"30/1","NAME":"CAM1",}
        if json_data:
            qrcode = json.loads(json_data.replace(',}', '}'))
        # Sometime plugin can not read QRcode information
        if len(json_data) > self._json_length:
            qrcode['ELEMENTNAME'] = elt_name
            qrcode['VIDEOTIMESTAMP'] = timestamp
            if qrcode.get('TICKFREQ'):
                logger.debug('qrcode found at timestamp %s, freq: %s' % (timestamp, qrcode['TICKFREQ']))
                self._qrcode_tickfreq_count += 1
            d = json.dumps(qrcode)
            self.write_line(d)
        else:
            logger.warning("Could not get content of qrcode")

    def evt_spectrum(self, event):
        elt_name = event.content['source']
        struct = event.content['data']
        timestamp = struct.get_value('timestamp')
        # FIXME: python does not support GValueList, parsing is required instead
        # https://bugzilla.gnome.org/show_bug.cgi?id=753754
        s = struct.to_string()
        magnitude = self.get_string_to_float_list(s)
        magnitude.pop(0)
        magnitude.pop(0)
        max_value = max(magnitude)
        if max_value > self._threshold_db:
            index = magnitude.index(max_value)
            nb_value = self._samplerate / 2.0 / self._bands
            freq = (index - 1) * nb_value + nb_value / 2.0
            if freq > self._min_freq:
                if freq == self._check_freq:
                    self._counter += 1
                else:
                    self._check_freq = freq
                    self._audio_timestamp = timestamp
                    self._counter = 0
                    self._magnitude_position = index
                    self._max_magnitude = max_value
                if self._counter == 5 and (float(self._audio_timestamp) - float(self._audio_timestamp_saved)) / 1000000000.0 >= 0.9:
                    self._audio_timestamp_saved = self._audio_timestamp
                    result = {
                        "ELEMENTNAME": elt_name,
                        "TIMESTAMP": self._audio_timestamp,
                        "PEAK": self._max_magnitude,
                        "FREQ": self._check_freq,
                    }
                    logger.debug("tick found at timestamp : %s, index : %s, freq : %d, peak  :%.1f" % (self._audio_timestamp, self._magnitude_position, self._check_freq, self._max_magnitude))
                    self._tick_count += 1
                    self.write_line(json.dumps(result))

    def get_media_info(self, media_file):
        try:
            ffprobe = shutil.which('ffprobe')
        except Exception:
            # python2
            from distutils.spawn import find_executable
            ffprobe = find_executable('ffprobe')
        if ffprobe:
            cmd = "ffprobe -v error -select_streams v -show_entries stream=width,height,avg_frame_rate,duration -of default=noprint_wrappers=1 -print_format json %s" % media_file
            result = subprocess.check_output(cmd.split(' '), universal_newlines=True)
            vjres = json.loads(result)['streams'][0]
            if not vjres.get('duration'):
                cmd = "ffprobe -v error -select_streams v -show_format_entry duration -of default=noprint_wrappers=1 -print_format json %s" % media_file
                result = subprocess.check_output(cmd.split(' '), universal_newlines=True)
                vjres['duration'] = json.loads(result)['format']['duration']
            cmd = "ffprobe -v error -select_streams a -show_entries stream=sample_rate -of default=noprint_wrappers=1 -print_format json %s" % media_file
            result = subprocess.check_output(cmd.split(' '), universal_newlines=True)
            ajres = json.loads(result)['streams']
            if ajres:
                ajres = ajres[0]
                vjres['sample_rate'] = ajres['sample_rate']
            else:
                logger.error("No audio track found, cannot detect sync")
            return vjres
        else:
            logger.error('ffprobe is required')
            sys.exit()

    def disconnect_probes(self):
        logger.debug('Disconnecting probes')
        if self._audio_fakesink_pad:
            self._audio_fakesink_pad.remove_probe(self._id_prob_audio_sink)
        if self._video_fakesink_pad:
            self._video_fakesink_pad.remove_probe(self._id_prob_video_sink)

    def get_string_to_float_list(self, string):
        my_list = list(re.search(r"magnitude=\(float\)([\d\- ,{}.]*)", string).group(1).strip("{}").split(","))
        my_list = [float(i.strip()) for i in my_list]
        return my_list

    def write_line(self, line):
        if line is not None:
            line += '\n'
            self._result_file.write(line)
            self._result_file.flush()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description='Generate videos suitable for measuring lipsync with qrcodes',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('input_file', help='filename of video to analyze')
    parser.add_argument('-v', '--verbosity', help='increase output verbosity', action="store_true")
    options = parser.parse_args()
    verbosity = getattr(logging, "DEBUG" if options.verbosity else "INFO")

    logging.basicConfig(
        level=verbosity,
        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
        stream=sys.stderr
    )

    media_file = options.input_file
    mainloop = GLib.MainLoop()
    if os.path.isfile(media_file):
        dirname = os.path.dirname(media_file)
        media_prefix = os.path.splitext(os.path.basename(media_file))[0]
        result_file = os.path.join(dirname, "%s_data.txt" % (media_prefix))
        d = QrLipsyncDetector(media_file, result_file, mainloop)
        GLib.idle_add(d.start)
        try:
            mainloop.run()
        except KeyboardInterrupt:
            logger.info('Ctrl+C hit, stopping')
            d.exit()
    else:
        logger.error("File %s not found" % media_file)
