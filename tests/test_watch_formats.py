"""Which video the bot downloads: browsers must be able to show it."""
import unittest

import yt_dlp

import watchtogether


def picked(formats):
    selector = watchtogether._dl_opts('/tmp', quality=720)['format']
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'format': selector, 'simulate': True}) as ydl:
        info = {'id': 'x', 'title': 'x', 'extractor': 't', 'extractor_key': 'T', 'webpage_url': 'http://x',
                'formats': [dict(f) for f in formats]}
        result = ydl.process_ie_result(info, download=False)
    return (result.get('requested_formats') or [result])[0]['format_id']


def video(format_id, codec, ext='mp4', height=720, tbr=2000):
    return {'format_id': format_id, 'url': 'http://x/' + format_id, 'ext': ext, 'vcodec': codec,
            'acodec': 'none', 'height': height, 'width': height * 16 // 9, 'tbr': tbr, 'protocol': 'https'}


AUDIO = {'format_id': '140', 'url': 'http://x/140', 'ext': 'm4a', 'vcodec': 'none',
         'acodec': 'mp4a.40.2', 'tbr': 129, 'protocol': 'https'}
AV1 = video('398', 'av01.0.05M.08', tbr=1200)
VP9_IN_MP4 = video('698', 'vp09.00.31.08', tbr=1500)
H264 = video('136', 'avc1.4D401F', tbr=2300)


class DownloadFormat(unittest.TestCase):
    def test_h264_beats_the_av1_and_vp9_youtube_also_puts_in_mp4(self):
        self.assertEqual(picked([AV1, VP9_IN_MP4, H264, AUDIO]), '136')

    def test_a_video_with_no_h264_still_plays_for_those_who_can(self):
        self.assertEqual(picked([AV1, AUDIO]), '398')


if __name__ == '__main__':
    unittest.main()
