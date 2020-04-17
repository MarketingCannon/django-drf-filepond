import logging
import os

from django.core.files.uploadedfile import UploadedFile, InMemoryUploadedFile
from rest_framework import status
from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from django_drf_filepond.models import TemporaryUpload, storage,\
    TemporaryUploadChunked
from django.contrib.auth.models import AnonymousUser
from io import BytesIO, StringIO

LOG = logging.getLogger(__name__)


# Get the user associated with the provided request. If we have an anonymous
# user object then return None
def _get_user(request):
    upload_user = getattr(request, 'user', None)
    if isinstance(upload_user, AnonymousUser):
        upload_user = None
    return upload_user


class FilepondFileUploader(object):

    @classmethod
    def get_uploader(cls, request):
        # Process the request to identify if it's a standard upload request
        # or a request that is related to a chunked upload. Return the right
        # kind of uploader to handle this.
        if request.method == 'PATCH':
            return FilepondChunkedFileUploader()
        if request.method == 'HEAD':
            return FilepondChunkedFileUploader()
        elif request.method == 'POST':
            file_obj = cls._get_file_obj(request)
            if (file_obj == '{}' and
                    request.META.get('HTTP_UPLOAD_LENGTH', None)):

                    LOG.debug('Returning CHUNKED uploader to handle '
                              'upload request... ')
                    return FilepondChunkedFileUploader()

        # If we didn't identify the need for a chunked uploader in any of the
        # above tests, treat this as a standard upload
        LOG.debug('Returning STANDARD uploader to handle upload request... ')
        return FilepondStandardFileUploader()

    @classmethod
    def _get_file_obj(cls, request):
        # By default the upload element name is expected to be "filepond"
        # As raised in issue #4, there are cases where there may be more
        # than one filepond instance on a page, or the developer has opted
        # not to use the name "filepond" for the filepond instance.
        # Using the example from #4, this provides support these cases.
        upload_field_name = 'filepond'
        if 'fp_upload_field' in request.data:
            upload_field_name = request.data['fp_upload_field']

        if upload_field_name not in request.data:
            raise ParseError("Invalid request data has been provided.")

        file_obj = request.data[upload_field_name]

        return file_obj


class FilepondStandardFileUploader(FilepondFileUploader):

    def handle_upload(self, request, upload_id, file_id):
        file_obj = self._get_file_obj(request)

        # Save original file name and set name of saved file to the unique ID
        upload_filename = file_obj.name
        file_obj.name = file_id

        # The type of parsed data should be a descendant of an UploadedFile
        # type.
        if not isinstance(file_obj, UploadedFile):
            raise ParseError('Invalid data type has been parsed.')

        # Before we attempt to save the file, make sure that the upload
        # directory we're going to save to exists.
        # *** It's not necessary to explicitly create the directory since
        # *** the FileSystemStorage object creates the directory on save
        # if not os.path.exists(storage.location):
        #    LOG.debug('Filepond app: Creating file upload directory '
        #             '<%s>...' % storage.location)
        #    os.makedirs(storage.location, mode=0o700)

        LOG.debug('About to store uploaded temp file with filename: %s'
                  % (upload_filename))

        # We now need to create the temporary upload object and store the
        # file and metadata.
        tu = TemporaryUpload(upload_id=upload_id, file_id=file_id,
                             file=file_obj, upload_name=upload_filename,
                             upload_type=TemporaryUpload.FILE_DATA,
                             uploaded_by=_get_user(request))
        tu.save()

        response = Response(upload_id, status=status.HTTP_200_OK,
                            content_type='text/plain')

        return response


class FilepondChunkedFileUploader(FilepondFileUploader):

    def handle_upload(self, request, upload_id, file_id=None):
        if request.method == 'PATCH':
            return self._handle_chunk_upload(request, upload_id)
        elif request.method == 'HEAD':
            return self._handle_chunk_restart(request, upload_id)
        elif request.method == 'POST':
            return self._handle_new_chunk_upload(request, upload_id, file_id)

    def _handle_new_chunk_upload(self, request, upload_id, file_id):
        LOG.debug('Processing a new chunked upload request...')
        file_obj = self._get_file_obj(request)
        if file_obj != '{}':
            return Response('An invalid file object has been received '
                            'for a new chunked upload request.',
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        ulen = request.META.get('HTTP_UPLOAD_LENGTH', None)
        if not ulen:
            return Response('No length for new chunked upload request.',
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        LOG.debug('Handling a new chunked upload request for an upload '
                  'with total length %s bytes' % (ulen))

        # Do some general checks to make sure that the storage location
        # exists and that we're not being made to try and store something
        # outside the base storage location. Then create the new
        # temporary directory into which chunks will be stored
        base_loc = storage.base_location
        chunk_dir = os.path.abspath(os.path.join(base_loc, upload_id))
        if not chunk_dir.startswith(base_loc):
            return Response('Unable to create storage for upload data.',
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        if os.path.exists(base_loc):
            try:
                os.makedirs(chunk_dir, exist_ok=False)
            except OSError as e:
                LOG.debug('Unable to create chunk storage dir: %s' %
                          (str(e)))
                return Response(
                    'Unable to prepare storage for upload data.',
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        else:
            LOG.debug('The base data store location <%s> doesn\'t exist.'
                      ' Unable to create chunk dir.' % (base_loc))
            return Response('Data storage error occurred.',
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # We now create the temporary chunked upload object
        # this will be updated as we receive the chunks.
        tuc = TemporaryUploadChunked(upload_id=upload_id, file_id=file_id,
                                     upload_dir=upload_id, total_size=ulen,
                                     uploaded_by=_get_user(request))
        tuc.save()

        return Response(upload_id, status=status.HTTP_200_OK,
                        content_type='text/plain')

    def _handle_chunk_upload(self, request, chunk_id):
        if (not chunk_id) or (chunk_id == ''):
            return Response('A required chunk parameter is missing.',
                            status=status.HTTP_400_BAD_REQUEST)

        # Try to load a temporary chunked upload object for the provided id
        try:
            tuc = TemporaryUploadChunked.objects.get(upload_id=chunk_id)
        except TemporaryUploadChunked.DoesNotExist:
            return Response('Invalid chunk upload request data',
                            status=status.HTTP_400_BAD_REQUEST)

        # Get the required header information to handle the new data
        uoffset = request.META.get('HTTP_UPLOAD_OFFSET', None)
        ulength = request.META.get('HTTP_UPLOAD_LENGTH', None)
        uname = request.META.get('HTTP_UPLOAD_NAME', None)

        if (not uoffset) or (not ulength) or (not uname):
            return Response('Chunk upload is missing required metadata',
                            status=status.HTTP_400_BAD_REQUEST)
        if int(ulength) != tuc.total_size:
            return Response('ERROR: Upload metadata is invalid - size changed',
                            status=status.HTTP_400_BAD_REQUEST)

        # if this is the first chunk, store the filename
        if tuc.last_chunk == 0:
            tuc.upload_name = uname
        else:
            if tuc.upload_name != uname:
                return Response('Chunk upload file metadata is invalid',
                                status=status.HTTP_400_BAD_REQUEST)

        LOG.debug('Handling chunk <%s> for upload id <%s> with name <%s> '
                  'size <%s> and offset <%s>...'
                  % (tuc.last_chunk+1, chunk_id, uname, ulength, uoffset))

        LOG.debug('Current length and offset in the record is: length <%s> '
                  '  offset <%s>' % (tuc.total_size, tuc.offset))

        # Check that our recorded offset matches the offset provided by the
        # client...if not, there's an error.
        if not (int(uoffset) == tuc.offset):
            LOG.error('Offset provided by client <%s> doesn\'t match the '
                      'stored offset <%s> for chunked upload id <%s>'
                      % (uoffset, tuc.offset, chunk_id))
            return Response('ERROR: Chunked upload metadata is invalid.',
                            status=status.HTTP_400_BAD_REQUEST)

        # Get the data and check it fits with the metadata and then save
        file_data = request.data
        file_data_len = len(file_data)
        LOG.debug('Got data from request with length %s bytes'
                  % (file_data_len))

        if type(file_data) == bytes:
            fd = BytesIO(file_data)
        elif type(file_data) == str:
            fd = StringIO(file_data)
        else:
            return Response('Upload data type not recognised.',
                            status=status.HTTP_400_BAD_REQUEST)

        # Store the chunk and check if we've now completed the upload
        upload_dir = os.path.join(storage.base_location, tuc.upload_dir)
        upload_file = os.path.join(tuc.upload_dir,
                                   '%s_%s' % (tuc.file_id, tuc.last_chunk+1))
        if not os.path.exists(upload_dir):
            return Response('Chunk storage location error',
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        storage.save(upload_file, fd)
        # Set the updated chunk number and the new offset
        tuc.last_chunk = tuc.last_chunk + 1
        tuc.offset = tuc.offset + file_data_len
        if tuc.offset == tuc.total_size:
            tuc.upload_complete = True
        tuc.save()

        # At this point, if the upload is complete, we can rebuild the chunks
        # into the complete file and store it with a TemporaryUpload object.
        if tuc.upload_complete:
            try:
                self._store_upload(tuc)
            except (ValueError, FileNotFoundError) as e:
                LOG.error('Error storing upload: %s' % (str(e)))
                return Response('Error storing uploaded file.',
                                status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(chunk_id, status=status.HTTP_200_OK,
                        content_type='text/plain')

    def _store_upload(self, tuc):
        if not tuc.upload_complete:
            LOG.error('Attempt to store an incomplete upload with ID <%s>'
                      % (tuc.upload_id))
            raise ValueError('Attempt to store an incomplete upload with ID '
                             '<%s>' % (tuc.upload_id))

        # Load each of the file parts into a BytesIO object and store them
        # via a TemporaryUpload object.
        chunk_dir = os.path.join(storage.base_location, tuc.upload_dir)
        file_data = BytesIO()
        for i in range(1, tuc.last_chunk+1):
            chunk_file = os.path.join(chunk_dir, '%s_%s' % (tuc.file_id, i))
            if not os.path.exists(chunk_file):
                raise FileNotFoundError('Chunk file not found for chunk <%s>'
                                        % (i))

            with open(chunk_file, 'rb') as cf:
                file_data.write(cf.read())

        # Prepare an InMemoryUploadedFile object so that the data can be
        # successfully saved via the FileField in the TemporaryUpload object
        memfile = InMemoryUploadedFile(file_data, None, tuc.file_id,
                                       'application/octet-stream',
                                       tuc.total_size, None)
        tu = TemporaryUpload(upload_id=tuc.upload_id, file_id=tuc.file_id,
                             file=memfile, upload_name=tuc.upload_name,
                             upload_type=TemporaryUpload.FILE_DATA,
                             uploaded_by=tuc.uploaded_by)
        tu.save()

        # Check that the final file is stored and of the correct size
        stored_file_path = os.path.join(chunk_dir, tuc.file_id)
        if ((not os.path.exists(stored_file_path)) or
                (not os.path.getsize(stored_file_path) == tuc.total_size)):
            raise ValueError('Stored file size wrong or file not found.')

        LOG.debug('Full file built from chunks and saved. Deleting chunks '
                  'and TemporaryUploadChunked object.')

        for i in range(1, tuc.last_chunk+1):
            chunk_file = os.path.join(chunk_dir, '%s_%s' % (tuc.file_id, i))
            os.remove(chunk_file)
        tuc.delete()

    def _handle_chunk_restart(self, request, upload_id):
        try:
            tuc = TemporaryUploadChunked.objects.get(upload_id=upload_id)
        except TemporaryUploadChunked.DoesNotExist:
            return Response('Invalid upload ID specified.',
                            status=status.HTTP_404_NOT_FOUND)
        # Check that the directory for the chunks exists
        if not os.path.exists(os.path.join(storage.base_location,
                                           tuc.upload_dir)):
            return Response('Invalid upload location, can\'t continue upload.',
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # TODO: Is it necessary to check for the existence of all previous
        #       chunk files here?
        LOG.debug('Returning offset to continue chunked upload. We have <%s> '
                  'chunks so far and are at offest <%s>.'
                  % (tuc.last_chunk, tuc.offset))
        return Response(upload_id, status=status.HTTP_200_OK,
                        headers={'Upload-Offset': str(tuc.offset)},
                        content_type='text/plain')
