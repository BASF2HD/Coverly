from sqlalchemy import select, func
from coverly.db import Job, JobItem, Page, Album, ImageAsset, AlbumImage, Restoration, OCRResult
from coverly.jobs import Worker, enqueue, enqueue_download, recover
from coverly.storage import within


def test_controlled_website_end_to_end(settings, db, website):
    factory, visited, _ = website
    worker = Worker(settings, db, factory)
    with db() as s:
        job = enqueue(s, 'discover', {'url':'https://records.example/tamil/', 'max_pages':10})
        job_id = job.id
        s.commit()
    assert worker.tick()
    with db() as s:
        assert s.get(Job, job_id).status == 'completed'
        pages = list(s.scalars(select(Page)))
        assert len(pages) == 2
        page = next(p for p in pages if 'nayakan' in p.url)
        job = enqueue_download(s, [page])
        download_id = job.id
        s.commit()
    assert worker.tick()
    with db() as s:
        assert s.get(Job, download_id).status == 'completed'
        assert s.scalar(select(func.count()).select_from(Album)) == 1
        assert s.scalar(select(func.count()).select_from(ImageAsset)) == 3
        assets = list(s.scalars(select(ImageAsset)))
        front = next(a for a in assets if a.source_url.endswith('/front.jpg'))
        assert front.width == 1200
        assert s.get(AlbumImage, (s.scalar(select(Album.id)), front.id)).classification == 'front'
        original = within(settings.data, front.original).read_bytes()
        restore = enqueue(s, 'restore', {'preset':'gentle','size':1000}, [(front.id,{})])
        restore_id = restore.id
        s.commit()
    assert worker.tick()
    with db() as s:
        assert s.get(Job, restore_id).status == 'completed'
        result = s.scalar(select(Restoration))
        assert result.status == 'pending'
        assert within(settings.data, result.path).is_file()
        assert within(settings.data, front.original).read_bytes() == original


def test_interrupted_recovery(settings, db, website):
    factory, _, _ = website
    with db() as s:
        job = enqueue(s, 'discover', {'url':'https://records.example/tamil/','max_pages':10,'queue':[['https://records.example/product/nayakan/',1]],'seen':['https://records.example/tamil/']})
        job.status='running'
        item=JobItem(job_id=job.id,key='https://records.example/product/nayakan/',status='running',data={'depth':1})
        s.add(item)
        s.commit()
        recover(s)
        assert job.status == 'queued'
        assert item.status == 'pending'
        identifier=job.id
    Worker(settings,db,factory).tick()
    with db() as s:
        assert s.get(Job,identifier).status == 'completed'
        assert s.scalar(select(func.count()).select_from(Page)) == 2


def test_download_and_restoration_failure(settings, db, website):
    factory, _, _ = website
    with db() as s:
        job=enqueue(s,'restore',{'preset':'gentle','size':1000},[('missing-image',{})])
        identifier=job.id
        s.commit()
    Worker(settings,db,factory).tick()
    with db() as s:
        assert s.get(Job,identifier).status == 'completed_with_errors'
        assert s.scalar(select(JobItem).where(JobItem.job_id==identifier)).error == 'Unknown image'
