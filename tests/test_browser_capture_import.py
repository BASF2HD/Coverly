from scripts.import_browser_capture import capture_files
from conftest import jpeg


def test_missing_original_uses_largest_actual_observed_variant(tmp_path):
    small = tmp_path / 'small.jpg'
    large = tmp_path / 'large.jpg'
    invalid = tmp_path / 'bad.jpg'
    small.write_bytes(jpeg(200, 300))
    large.write_bytes(jpeg(800, 700))
    invalid.write_bytes(b'not an image')
    original = 'https://records.example/missing.jpg'
    capture = {'url':'https://records.example/product/album/', 'images':[{'url':original}], 'files':[],
               'fallback_candidates':{original:[{'url':'https://records.example/bad.jpg','path':str(invalid)},
                                               {'url':'https://records.example/small.jpg','path':str(small)},
                                               {'url':'https://records.example/large.jpg','path':str(large)}]}}
    files = capture_files(capture)
    assert len(files) == 1 and files[0]['path'] == str(large)
    assert files[0]['fallback_for'] == original
    assert not capture['_missing_photos']


def test_missing_photo_is_reported_without_discarding_available_photo():
    available = {'url':'https://records.example/available.jpg','path':'unused'}
    capture = {'url':'https://records.example/product/album/',
               'images':[{'url':'https://records.example/missing.jpg'},{'url':available['url']}], 'files':[available]}
    assert capture_files(capture) == [available]
    assert capture['_missing_photos'] == ['https://records.example/missing.jpg']
