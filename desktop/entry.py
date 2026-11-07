"""Keep freeze_support ahead of GUI imports for frozen spawn children."""
import multiprocessing

if __name__ == '__main__':
    from coverly.bootstrap import prepare_streams
    prepare_streams()
    multiprocessing.freeze_support()
    from coverly.desktop import main
    main()
