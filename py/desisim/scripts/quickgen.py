from __future__ import absolute_import, division, print_function

import argparse
from astropy.io import fits
from time import asctime

import desisim
import desispec.io
from desispec.log import get_logger
log = get_logger()

import os
import os.path
import numpy as np
import scipy.special
import scipy.interpolate
import sys
import desispec
import desisim.io
import specsim.simulator
import astropy.units as u
from desispec.resolution import Resolution
from desispec.io import write_flux_calibration, write_fiberflat, read_fibermap, specprod_root
from desispec.interpolation import resample_flux
from desispec.frame import Frame
from desispec.fiberflat import FiberFlat
from desispec.sky import SkyModel
from desispec.fluxcalibration import FluxCalib
from desispec.log import INFO
from ..specsim import get_simulator

def expand_args(args):
    hdr = fits.getheader(args.simspec)
    night = str(hdr['NIGHT'])
    expid = int(hdr['EXPID'])
    if args.simspec is None:
        if args.fibermap is None:
            msg = 'Must set --simspec and --fibermap'
            log.error(msg)
            raise ValueError(msg)
        args.simspec = desisim.io.findfile('simspec', night, expid)
        args.fibermap = desispec.io.findfile('fibermap', night, expid)

def parse(options=None):
    parser=argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument("--simspec",type=str, help="input simspec file")
    parser.add_argument("--fibermap",type=str, help='input fibermap file')
    parser.add_argument("--nspec",type=int,default=5000,help='no. of spectra to be simulated, starting from first')
    parser.add_argument("--nstart", type=int, default=0,help='starting spectra # for simulation 0-4999')
    parser.add_argument("--spectrograph",type=int, default=None,help='Spectrograph no. 0-9')
    parser.add_argument("--config", type=str, default='desi', help='specsim configuration')
    parser.add_argument("--seed", type=int, default=0,  help="random seed")
    parser.add_argument("--frameonly", action="store_true", help="only output frame files")
    parser.add_argument('--moon-phase', type=float,  help='moon phase (0=full, 1=new)', default=None, metavar='')
    parser.add_argument('--moon-angle', type=float,  help='separation angle to the moon (0-180 deg)', default=None, metavar='')
    parser.add_argument('--moon-zenith', type=float,  help='zenith angle of the moon (0-90 deg)', default=None, metavar='')

    if options is None:
        args = parser.parse_args()
    else:
        options = [str(x) for x in options]
        args = parser.parse_args(options)

    expand_args(args)
    return args

def main(args=None):
    if isinstance(args, (list, tuple, type(None))):
        args = parse(args)

    # Initialize random number generator to use.
    random_state = np.random.RandomState(args.seed)

    # Derive spectrograph number from nstart if needed
    if args.spectrograph is None:
        args.spectrograph = args.nstart / 500

    # Look for Directory tree/ environment set up
    # Directory Tree is $DESI_SPECTRO_REDUX/$PRODNAME/exposures/NIGHT/EXPID/*.fits
    # Perhaps can be synced with desispec findfile?
    # But read fibermap file and extract the headers needed for Directory tree

    # read fibermapfile to get objecttype,NIGHT and EXPID....
    if args.fibermap:

        log.info("Reading fibermap file {}".format(args.fibermap))
        fibermap=read_fibermap(args.fibermap)
        objtype=fibermap['OBJTYPE'].copy()
        #need to replace STD and MWS_STAR object types with STAR and BGS object types with LRG since quicksim expects star instead of std or mws_star and LRG instead of BGS
        stdindx=np.where(objtype=='STD') # match STD with STAR
        mwsindx=np.where(objtype=='MWS_STAR') # match MWS_STAR with STAR
        bgsindx=np.where(objtype=='BGS') # match BGS with LRG
        objtype[stdindx]='STAR'
        objtype[mwsindx]='STAR'
        objtype[bgsindx]='LRG'
        NIGHT=fibermap.meta['NIGHT']
        EXPID=fibermap.meta['EXPID']


    else:
        log.info("Need Fibermap file")
        sys.exit(1)


    #----------DESI_SPECTRO_REDUX--------
    DESI_SPECTRO_REDUX_DIR="./quickGen"

    if 'DESI_SPECTRO_REDUX' not in os.environ:

        log.info('DESI_SPECTRO_REDUX environment is not set.')

    else:
        DESI_SPECTRO_REDUX_DIR=os.environ['DESI_SPECTRO_REDUX']

    if os.path.exists(DESI_SPECTRO_REDUX_DIR):

        if not os.path.isdir(DESI_SPECTRO_REDUX_DIR):
            raise RuntimeError("Path %s Not a directory"%DESI_SPECTRO_REDUX_DIR)
    else:
        try:
            os.makedirs(DESI_SPECTRO_REDUX_DIR)
        except:
            raise

    #---------SPECPROD-----------------

    SPECPROD_DIR='specprod'
    if 'SPECPROD' not in os.environ:
        log.info('SPECPROD environment is not set.')
    else:
        SPECPROD_DIR=os.environ['SPECPROD']
    prod_Dir=specprod_root()

    if os.path.exists(prod_Dir):

        if not os.path.isdir(prod_Dir):
            raise RuntimeError("Path %s Not a directory"%prod_Dir)
    else:
        try:
            os.makedirs(prod_Dir)
        except:
            raise

    # read the input file (simspec file)

    log.info('Now Reading the input file {}'.format(args.simspec))
    simspec = desisim.io.read_simspec(args.simspec)
    if simspec.flavor == 'arc':
        pass
    else:
        wavelengths = simspec.wave['brz']
        spectra = simspec.flux

    # Note spectra=data/1.0e-17# flux in units of 1.0e-17 ergs/cm^2/s/A

        log.info("Wavelength range: {} to {}".format(wavelengths[0],wavelengths[-1]))
        nwave = len(wavelengths)

    nspec = simspec.nspec
    if nspec < args.nspec:
        log.info("Only {} spectra in input file".format(nspec))
        args.nspec = nspec

    # Here default run for nmax spectra. Fewer spectra can be run using 'nspec' and 'nstart' options
    nmax= min(args.nspec+args.nstart,objtype.shape[0])
    log.info("Simulating spectra {} to {}".format(args.nstart,nmax))

    log.info("************************************************")

    log.info("Initializing SpecSim with config {}".format(args.config))
    ### qsim = specsim.simulator.Simulator(args.config)
    qsim = get_simulator(args.config)

    # explicitly set location on focal plane if needed to support airmass
    # variations when using specsim v0.5
    if qsim.source.focal_xy is None:
        qsim.source.focal_xy = (u.Quantity(0, 'mm'), u.Quantity(100, 'mm'))

    # Set simulation parameters from the simspec header.
    qsim.atmosphere.airmass = simspec.header['AIRMASS']
    qsim.observation.exposure_time = simspec.header['EXPTIME'] * u.s

    # Set moon parameters
    if simspec.flavor == 'bgs' or simspec.flavor == 'mws' or simspec.flavor == 'bright':
        if args.moon_phase is None:
            qsim.atmosphere.moon.moon_phase = 0.7
        else:
            qsim.atmosphere.moon.moon_phase = args.moon_phase
        if args.moon_angle is None:
            qsim.atmosphere.moon.separation_angle = 50 * u.deg
        else:
            qsim.atmosphere.moon.separation_angle = args.moon_angle * u.deg
        if args.moon_zenith is None:
            qsim.atmosphere.moon.moon_zenith = 30 * u.deg
        else:
            qsim.atmosphere.moon.moon_zenith = args.moon_zenith * u.deg
    elif simspec.flavor == 'gray' or simspec.flavor == 'grey':
        if args.moon_phase is None:
            qsim.atmosphere.moon.moon_phase = 0.1
        else:
            qsim.atmosphere.moon.moon_phase = args.moon_phase
        if args.moon_angle is None:
            qsim.atmosphere.moon.separation_angle = 60 * u.deg
        else:
            qsim.atmosphere.moon.separation_angle = args.moon_angle * u.deg
        if args.moon_zenith is None:
            qsim.atmosphere.moon.moon_zenith = 80 * u.deg
        else:
            qsim.atmosphere.moon.moon_zenith = args.moon_zenith * u.deg
    else:
        qsim.atmosphere.moon.moon_phase = 0.5
        qsim.atmosphere.moon.separation_angle = 60 * u.deg
        qsim.atmosphere.moon.moon_zenith = 100 * u.deg

    # Get the camera output pixels from the specsim instrument model.
    maxbin = 0
    waves=dict()
    for i,camera in enumerate(qsim.instrument.cameras):
        channel = camera.name
        assert camera.output_wavelength.unit == u.Angstrom
        waves[channel] = camera.output_wavelength.value
        maxbin = max(maxbin, len(waves[channel]))

    #- Check if input simspec is for a continuum flat lamp instead of science
    #- This does not convolve to per-fiber resolution
    if simspec.flavor == 'flat':
        log.info("Simulating flat lamp exposure")
        for i,camera in enumerate(qsim.instrument.cameras):
            channel = camera.name
            assert camera.output_wavelength.unit == u.Angstrom
            num_pixels = len(waves[channel])
            dw = np.gradient(simspec.wave[channel])
            meanspec = resample_flux(
                waves[channel], simspec.wave[channel],
                np.average(simspec.phot[channel]/dw, axis=0))
            fiberflat = random_state.normal(loc=1.0, 
                scale=1.0 / np.sqrt(meanspec), size=(nspec, num_pixels))
            ivar = np.tile(1.0 / meanspec, [nspec, 1])
            mask = np.zeros((simspec.nspec, num_pixels), dtype=np.uint32)

            for kk in range((args.nspec+args.nstart-1)//500+1):
                camera = channel+str(kk)
                outfile = desispec.io.findfile('fiberflat', NIGHT, EXPID, camera)
                start=max(500*kk,args.nstart)
                end=min(500*(kk+1),nmax)

                if (args.spectrograph <= kk):
                    log.info("writing files for channel:{}, spectrograph:{}, spectra:{} to {}".format(channel,kk,start,end))

                ff = FiberFlat(
                    waves[channel], fiberflat[start:end,:],
                    ivar[start:end,:], mask[start:end,:], meanspec,
                    header=dict(CAMERA=camera))
                write_fiberflat(outfile, ff)
                filePath=desispec.io.findfile("fiberflat",NIGHT,EXPID,camera)
                log.info("Wrote file {}".format(filePath))

        sys.exit(0)

    elif simspec.flavor =='arc':
        # note: treating fiberloss as perfect and electrons/s as photons/s
        import scipy.constants as const
        log.info("Simulating arc line exposure")

        #- create full wavelength and flux arrays for arc exposure
        wave_b = np.array(simspec.wave['b'])
        wave_r = np.array(simspec.wave['r'])
        wave_z = np.array(simspec.wave['z'])
        phot_b = np.array(simspec.phot['b'][0])
        phot_r = np.array(simspec.phot['r'][0])
        phot_z = np.array(simspec.phot['z'][0])
        sim_wave = np.concatenate((wave_b,wave_r,wave_z))
        sim_phot = np.concatenate((phot_b,phot_r,phot_z))
        wavelengths = np.arange(3533.,9913.1,0.2)
        phot = np.zeros(len(wavelengths))
        for i in range(len(sim_wave)):
            wavelength = sim_wave[i]
            flux_index = np.argmin(abs(wavelength-wavelengths))
            phot[flux_index] = sim_phot[i]

        #- convert photons to flux: following specter conversion method
        dw = np.gradient(wavelengths)
        exptime = 5. # typical BOSS exposure time in s
        fibarea = const.pi*(1.07e-2/2)**2 # cross-sectional fiber area in cm^2
        hc = 1.e17*const.h*const.c # convert to erg A
        spectra = (hc*exptime*fibarea*dw*phot)/wavelengths
        nobj=np.zeros((nmax,3,maxbin))     # arc photons
        nivar=np.zeros((nmax,3,maxbin))     # inverse variance
        frame_rand_noise=np.zeros((nmax,3,maxbin))     # random Gaussian noise to nobj

        # resolution data in format as desired for frame file
        nspec=args.nspec
        resolution_data = dict()
        for i, channel in enumerate('brz'):
            log.info('Getting resolution matrix for channel {}'.format(channel))
            resolution_matrix = Resolution(
                qsim.instrument.cameras[i].get_output_resolution_matrix())
            log.info('...got it')
            resolution_data[channel] = np.tile(
                resolution_matrix.to_fits_array(), [nspec, 1, 1])

        fluxunits = u.erg / (u.s * u.cm ** 2 * u.Angstrom)
        for j in range(args.nstart,nmax): # Exclusive
            sys.stdout.flush()
            qsim.source.update_in(
                'Quickgen source {0}'.format, 'perfect',
                wavelengths * u.Angstrom, spectra * fluxunits)
            qsim.source.update_out()
            qsim.simulate()
            qsim.generate_random_noise(random_state)

            for i, output in enumerate(qsim.camera_output):

                # Extract the simulation results needed to create our uncalibrated
                # frame output file (only frame file needed for arc).
                num_pixels = len(output)
                nobj[j, i, :num_pixels] = output['num_source_electrons']
                frame_rand_noise[j, i, :num_pixels] = output['random_noise_electrons']

        armName={"b":0,"r":1,"z":2}
        for channel in 'brz':

            #Before writing, convert from counts/bin to counts/A (as in Pixsim output)
            #Quicksim Default:
            #FLUX - input spectrum resampled to this binning; no noise added [1e-17 erg/s/cm2/s/Ang]
            #COUNTS_OBJ - object counts in 0.5 Ang bin
    
            num_pixels = len(waves[channel])
            dwave=np.gradient(waves[channel])
            nobj[:,armName[channel],:num_pixels]/=dwave
            frame_rand_noise[:,armName[channel],:num_pixels]/=dwave
            nivar[:,armName[channel],:num_pixels]*=dwave**2

        # Looping over spectrograph

            for ii in range((args.nspec+args.nstart-1)//500+1):

                start=max(500*ii,args.nstart) # first spectrum for a given spectrograph
                end=min(500*(ii+1),nmax) # last spectrum for the spectrograph

                if (args.spectrograph <= ii):
                    camera = "{}{}".format(channel, ii)
                    log.info("writing files for channel:{}, spectrograph:{}, spectra:{} to {}".format(channel,ii,start,end))
                    num_pixels = len(waves[channel])

                    framefileName=desispec.io.findfile("frame",NIGHT,EXPID,camera)

                    frame_flux=nobj[start:end,armName[channel],:num_pixels]+ \
                    frame_rand_noise[start:end,armName[channel],:num_pixels]
                    frame_ivar=nivar[start:end,armName[channel],:num_pixels]

                    sh1=frame_flux.shape[0]  # required for slicing the resolution metric, resolusion matrix has (nspec,ndiag,wave)
                    if (args.nstart==start):
                        resol=resolution_data[channel][:sh1,:,:]
                    else:
                        resol=resolution_data[channel][-sh1:,:,:]

                    # create frame file. first create desispec.Frame object
                    frame=Frame(waves[channel],frame_flux,frame_ivar,resolution_data=resol,spectrograph=ii)
                    desispec.io.write_frame(framefileName, frame)

                    filePath=desispec.io.findfile("frame",NIGHT,EXPID,camera)
                    log.info("Wrote file {}".format(filePath))

        sys.exit(0)


    # Now break the simulated outputs in three different ranges.

    nobj=np.zeros((nmax,3,maxbin))     # Object Photons
    nsky=np.zeros((nmax,3,maxbin))      # sky photons
    nivar=np.zeros((nmax,3,maxbin))     # inverse variance  (object+sky)
    sky_ivar=np.zeros((nmax,3,maxbin)) # inverse variance of sky
    cframe_observedflux=np.zeros((nmax,3,maxbin))    # calibrated object flux
    cframe_ivar=np.zeros((nmax,3,maxbin))    # inverse variance of calibrated object flux
    frame_rand_noise=np.zeros((nmax,3,maxbin))     # random Gaussian noise to nobj+nsky
    sky_rand_noise=np.zeros((nmax,3,maxbin))  # random Gaussian noise to sky only
    cframe_rand_noise=np.zeros((nmax,3,maxbin))  # random Gaussian noise to calibrated flux


    #-------------------------------------------------------------------------

    # resolution data in format as desired for frame file
    nspec=args.nspec
    resolution_data = dict()
    for i, channel in enumerate('brz'):
        resolution_matrix = Resolution(
            qsim.instrument.cameras[i].get_output_resolution_matrix())
        resolution_data[channel] = np.tile(
            resolution_matrix.to_fits_array(), [nspec, 1, 1])

    # Now repeat the simulation for all spectra
    fluxunits = 1e-17 * u.erg / (u.s * u.cm ** 2 * u.Angstrom)
    for j in range(args.nstart,nmax): # Exclusive
        log.info("\rSimulating spectrum {},  object type= {}".format(j,objtype[j]))
        sys.stdout.flush()
        qsim.source.update_in(
            'Quickgen source {0}'.format(j), objtype[j].lower(),
            wavelengths * u.Angstrom, spectra[j, :] * fluxunits)
        qsim.source.update_out()
        qsim.simulate()
        qsim.generate_random_noise(random_state)

        for i, output in enumerate(qsim.camera_output):

            # Extract the simulation results needed to create our uncalibrated
            # frame output file.
            num_pixels = len(output)
            nobj[j, i, :num_pixels] = output['num_source_electrons']
            nsky[j, i, :num_pixels] = output['num_sky_electrons']
            nivar[j, i, :num_pixels] = 1.0 / output['variance_electrons']

            # Get results for our flux-calibrated output file.
            assert output['observed_flux'].unit == u.erg / (u.cm**2 * u.s * u.Angstrom)
            cframe_observedflux[j, i, :num_pixels] = 1e17 * output['observed_flux']
            cframe_ivar[j, i, :num_pixels] = 1e-34 * output['flux_inverse_variance']

            # Use the same noise realization in the cframe and frame, without any
            # additional noise from sky subtraction for now.
            frame_rand_noise[j, i, :num_pixels] = output['random_noise_electrons']
            cframe_rand_noise[j, i, :num_pixels] = 1e17 * (
                output['flux_calibration'] * output['random_noise_electrons'])

            # The sky output file represents a model fit to ~40 sky fibers.
            # We reduce the variance by a factor of 25 to account for this and
            # give the sky an independent (Gaussian) noise realization.
            sky_ivar[j, i, :num_pixels] = 25.0 / (
                output['variance_electrons'] - output['num_source_electrons'])
            sky_rand_noise[j, i, :num_pixels] = random_state.normal(
                scale=1.0 / np.sqrt(sky_ivar[j,i,:num_pixels]),size=num_pixels)


    armName={"b":0,"r":1,"z":2}

    #Need Four Files to write:
    #1. frame file: (x3)
    #2. skymodel file:(x3)
    #3. flux calibration vector file (x3)
    #4. cframe file

    for channel in 'brz':

        #Before writing, convert from counts/bin to counts/A (as in Pixsim output)
        #Quicksim Default:
        #FLUX - input spectrum resampled to this binning; no noise added [1e-17 erg/s/cm2/s/Ang]
        #COUNTS_OBJ - object counts in 0.5 Ang bin
        #COUNTS_SKY - sky counts in 0.5 Ang bin

        num_pixels = len(waves[channel])
        dwave=np.gradient(waves[channel])
        nobj[:,armName[channel],:num_pixels]/=dwave
        frame_rand_noise[:,armName[channel],:num_pixels]/=dwave
        nivar[:,armName[channel],:num_pixels]*=dwave**2

        nsky[:,armName[channel],:num_pixels]/=dwave
        sky_rand_noise[:,armName[channel],:num_pixels]/=dwave
        sky_ivar[:,armName[channel],:num_pixels]/=dwave**2


    #### Now write the outputs in DESI standard file system.    None of the output file can have more than 500 spectra

    # Looping over spectrograph

        for ii in range((args.nspec+args.nstart-1)//500+1):

            start=max(500*ii,args.nstart) # first spectrum for a given spectrograph
            end=min(500*(ii+1),nmax) # last spectrum for the spectrograph

            if (args.spectrograph <= ii):
                camera = "{}{}".format(channel, ii)
                log.info("writing files for channel:{}, spectrograph:{}, spectra:{} to {}".format(channel,ii,start,end))
                num_pixels = len(waves[channel])

    ######----------------frame file-----------------------------------

                framefileName=desispec.io.findfile("frame",NIGHT,EXPID,camera)

                frame_flux=nobj[start:end,armName[channel],:num_pixels]+ \
                nsky[start:end,armName[channel],:num_pixels] + \
                frame_rand_noise[start:end,armName[channel],:num_pixels]
                frame_ivar=nivar[start:end,armName[channel],:num_pixels]

                sh1=frame_flux.shape[0]  # required for slicing the resolution metric, resolusion matrix has (nspec,ndiag,wave)
                                          # for example if nstart =400, nspec=150: two spectrographs:
                                          # 400-499=> 0 spectrograph, 500-549 => 1
                if (args.nstart==start):
                    resol=resolution_data[channel][:sh1,:,:]
                else:
                    resol=resolution_data[channel][-sh1:,:,:]

                # create frame file. first create desispec.Frame object
                frame=Frame(waves[channel], frame_flux, frame_ivar,\
                    resolution_data=resol, spectrograph=ii, \
                    fibermap=fibermap[start:end], meta=dict(CAMERA=camera) )
                desispec.io.write_frame(framefileName, frame)

                framefilePath=desispec.io.findfile("frame",NIGHT,EXPID,camera)
                log.info("Wrote file {}".format(framefilePath))

                if args.frameonly:
                    continue

    ############--------------------------------------------------------
        #cframe file

                cframeFileName=desispec.io.findfile("cframe",NIGHT,EXPID,camera)
                cframeFlux=cframe_observedflux[start:end,armName[channel],:num_pixels]+cframe_rand_noise[start:end,armName[channel],:num_pixels]
                cframeIvar=cframe_ivar[start:end,armName[channel],:num_pixels]

                # write cframe file
                cframe = Frame(waves[channel], cframeFlux, cframeIvar, \
                    resolution_data=resol, spectrograph=ii,
                    fibermap=fibermap[start:end], meta=dict(CAMERA=camera) )
                desispec.io.frame.write_frame(cframeFileName,cframe)

                cframefilePath=desispec.io.findfile("cframe",NIGHT,EXPID,camera)
                log.info("Wrote file {}".format(cframefilePath))

    ############-----------------------------------------------------
                #sky file

                skyfileName=desispec.io.findfile("sky",NIGHT,EXPID,camera)
                skyflux=nsky[start:end,armName[channel],:num_pixels] + \
                sky_rand_noise[start:end,armName[channel],:num_pixels]
                skyivar=sky_ivar[start:end,armName[channel],:num_pixels]
                skymask=np.zeros(skyflux.shape, dtype=np.uint32)

                # write sky file
                skymodel = SkyModel(waves[channel], skyflux, skyivar, skymask,
                    header=dict(CAMERA=camera))
                desispec.io.sky.write_sky(skyfileName, skymodel)

                skyfilePath=desispec.io.findfile("sky",NIGHT,EXPID,camera)
                log.info("Wrote file {}".format(skyfilePath))

    ############----------------------------------------------------------
                 # calibration vector file

                calibVectorFile=desispec.io.findfile("calib",NIGHT,EXPID,camera)
                flux = cframe_observedflux[start:end,armName[channel],:num_pixels]
                phot = nobj[start:end,armName[channel],:num_pixels]
                calibration = np.zeros_like(phot)
                jj = (flux>0)
                calibration[jj] = phot[jj] / flux[jj]

        #- TODO: what should calibivar be?
        #- For now, model it as the noise of combining ~10 spectra
                calibivar=10/cframe_ivar[start:end,armName[channel],:num_pixels]
                #mask=(1/calibivar>0).astype(int)??
                mask=np.zeros(calibration.shape, dtype=np.uint32)

               # write flux calibration
                fluxcalib = FluxCalib(waves[channel], calibration, calibivar, mask)
                write_flux_calibration(calibVectorFile, fluxcalib)

                calibfilePath=desispec.io.findfile("calib",NIGHT,EXPID,camera)
                log.info("Wrote file {}".format(calibfilePath))

    #spectrograph=spectrograph+1

