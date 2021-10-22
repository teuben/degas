#from spectral_stack import stacking #need to make sure Erik's code is installed
from spectral_cube import SpectralCube, Projection
import numpy as np
import numpy.ma as ma
import astropy.units as u
import matplotlib.pyplot as plt
from astropy.io import fits
from matplotlib import colors
import aplpy
from astropy.table import Table, Column, vstack, QTable, MaskedColumn
import re
import glob 
from astropy.wcs import WCS
import os

import ipdb

def makeSampleTable(regridDir, outDir, scriptDir, vtype='mom1', outname='test', release='DR1', sourceList=None):
    '''

    Calculate stacking results for each galaxy and make a fits table
    of the results.

    Inputs:

        regridDir: directory containing regridded data

        scriptDir: directory with DEGAS data base

        outDir: directory for output results.

        vtype: velocity type. Options are 'mom1' and 'peakVelocity'. Default: 'mom1'
    
        outname: basename for output fits table.


    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    10/30/2020  A.A. Kepley     Added comments and clarified inputs
    12/3/2020   A.A. Kepley     Modified to pull list of galaxies from 
                                degas base table.

    '''
    
    from datetime import datetime

    # get list of dr1 galaxies
    degas = Table.read(os.path.join(scriptDir,'degas_base.fits'))
    idx = degas[release] == 1

    if not sourceList:
        sourceList = degas[idx]['NAME']

    # go though each file, create a table, and attend it to the list of tables.
    tablelist=[]
    for galaxy in degas[idx]:
        if galaxy['NAME'] in sourceList:
            full_tab = makeGalaxyTable(galaxy, vtype, regridDir, outDir)
            tablelist.append(full_tab)

    # stack all the tables together
    table=vstack(tablelist)

    table.meta['DATE'] = str(datetime.now())
    table.meta['RELEASE'] = release

    # Write out the table 
    table.write(os.path.join(outDir,outname+'_'+vtype+'.fits'),overwrite=True)

    return table

def makeGalaxyTable(galaxy, vtype, regridDir, outDir):
    '''

    make fitstable containing all lines from stacking results for each galaxy

    galaxy: data for galaxy we are processing from degas_base.fits

    vtype: velocity type that we are stacking on
    
    scriptDir: script directory -- AAK: if I read in degas_base.fits early,
    so I still need to pass this down.?
    
    regridDir: regrid directory
    
    outDir: output directory

    
    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/3/2020   A.A. Kepley     Added comments
    4/29/2021   A.A. Kepley     Moved all calculations for galaxy here 
                                instead of repeating per line.
    5/6/2021    A.A. Kepley     Modified so that all lines are calculated at once.

    '''

    print("Processing " + galaxy['NAME'] + "\n")

    # Create associated maps needed for analysis.
    # TODO -- double-check on mom0cut -- Is this really what i want to be doing??
    ## FIX UP THIS TO READ IN TO MOM0 AND ASSOCIATED ERROR PRODUCED VIA ANOTHER ROUTE.
    cubeCO, comap = mapCO(galaxy, regridDir, outDir)  
    stellarmap = mapStellar(galaxy, regridDir, outDir) # to apply CO mask mask=mom0cut
    sfrmap = mapSFR(galaxy, regridDir, outDir)
    ltirmap = mapLTIR(galaxy, regridDir,outDir)
    R_arcsec, R_kpc, R_r25 = mapGCR(galaxy, comap) # comap is just used to get coordinates

    velocity_file = galaxy['NAME']+'_12CO_'+vtype+'_regrid.fits' 
    vhdu = fits.open(os.path.join(regridDir,velocity_file))
    velocity = Projection.from_hdu(vhdu)

    # read in HCN
    linefile = glob.glob(os.path.join(regridDir,galaxy['NAME']+'_HCN_*_hanning1_maxnchan_smooth.fits'))[0]
    cubeHCN = SpectralCube.read(os.path.join(regridDir,linefile))
    
    # read in HCO+
    linefile = glob.glob(os.path.join(regridDir,galaxy['NAME']+'_HCOp_*_hanning1_smooth_regrid.fits'))[0]
    cubeHCOp = SpectralCube.read(os.path.join(regridDir,linefile))

    # For NGC6946, skip 13CO and C18O since we don't have that data.
    # For NGC4569, we are temporarily missing data.
    if (galaxy['NAME'] != 'NGC6946') & (galaxy['NAME'] != 'NGC4569'):
        # read in 13CO
        linefile = glob.glob(os.path.join(regridDir,galaxy['NAME']+'_13CO_*_hanning1_smooth_regrid.fits'))[0]
        cube13CO = SpectralCube.read(os.path.join(regridDir,linefile))

        # read in C18O
        linefile = glob.glob(os.path.join(regridDir,galaxy['NAME']+'_C18O_*_hanning1_smooth_regrid.fits'))[0]
        cubeC18O = SpectralCube.read(os.path.join(regridDir,linefile))
        
    else:
        cube13CO = None
        cubeC18O = None

    #get the full stack result for each line
    full_stack = makeStack(galaxy, regridDir, outDir,
                           cubeCO = cubeCO,
                           cubeHCN = cubeHCN, cubeHCOp=cubeHCOp,
                           cube13CO = cube13CO, cubeC18O = cubeC18O,
                           velocity = velocity,
                           comap = comap, 
                           sfrmap = sfrmap, ltirmap = ltirmap,
                           stellarmap=stellarmap, R_arcsec=R_arcsec,
                           R_r25=R_r25) 

    # remove stacks that don't have CO spectra 
    nstack = len(full_stack)
    keepstack = np.full(nstack,True)
    
    for i in range(nstack):
        if np.all(full_stack['stack_profile_CO'][i] == 0):
            keepstack[i] = False

    full_stack = full_stack[keepstack]

    # create directory for spectral line fits
    fit_plot_dir =  os.path.join(outDir,'spec_fits')
    if not os.path.exists(fit_plot_dir):
        os.mkdir(fit_plot_dir)

    # calculate integrated intensity
    full_stack = addIntegratedIntensity(full_stack, fit_plot_dir)

    # return the table and the stack.
    return full_stack


def makeStack(galaxy, regridDir, outDir,
              cubeCO = None,
              cubeHCN = None, cubeHCOp=None,
              cube13CO = None, cubeC18O = None,
              velocity = None, comap = None,
              sfrmap = None, ltirmap = None,
              stellarmap = None, R_arcsec = None,
              R_r25 = None):
    '''
    
    make stacks for all lines and ancillary data for one galaxy
    output python dictionaries

    galaxy: degas_base.fits entry for galaxy we are processing
    
    line: line we are stacking on
    
    regridDir: directory with all the regridded data

    outDir: directory with all the output data.

    ASSUMPTIONS: 

    We are assuming that all linecubes have been smoothed 
    and regridded at this point, so the coordinate systems match.


     Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/3/2020   A.A. Kepley     Added more comments plus minor changes to 
                                use the degas table 
    12/10/2020  A.A. Kepley     Added LTIR
    4/29/2021   A.A. Kepley     Moved up map calculations to makeStack to 
                                avoid doing multiple times.

    '''
 
    ## create radius bins
    binmap, binedge, binlabels = makeRadiusBins(galaxy, R_arcsec, outDir) 
    plotBins(galaxy, binmap, binedge, binlabels, 'radius', outDir) 


    # stack on radius
    stack_radius = stackLines(galaxy, velocity,
                          binmap, binedge,  'radius',  
                          cubeCO = cubeCO,
                          cubeHCN = cubeHCN, cubeHCOp = cubeHCOp,
                          cube13CO = cube13CO, cubeC18O = cubeC18O,
                          sfrmap = sfrmap, ltirmap = ltirmap, 
                          stellarmap = stellarmap)

    stack_radius.add_column(Column(np.tile('radius',len(stack_radius))),name='bin_type',index=0)

    # create r25 bins
    binmap, binedge, binlabels = makeRadiusBins(galaxy, R_r25, outDir,r25=True)
    plotBins(galaxy, binmap, binedge, binlabels, 'r25', outDir)


    # stack on R25
    stack_r25 = stackLines(galaxy, velocity,
                     binmap, binedge,  'r25',  
                     cubeCO = cubeCO,
                     cubeHCN = cubeHCN, cubeHCOp = cubeHCOp,
                     cube13CO = cube13CO, cubeC18O = cubeC18O,
                     sfrmap = sfrmap, ltirmap = ltirmap, 
                     stellarmap = stellarmap)

    stack_r25.add_column(Column(np.tile('r25',len(stack_r25))),name='bin_type',index=0)
    


    ## create stellar mass bin
    binmap, binedge, binlabels = makeStellarmassBins(galaxy, stellarmap, 'stellarmass', outDir)  
    plotBins(galaxy, binmap, binedge, binlabels, 'stellarmass', outDir) 


    ## do stellar mass stack
    stack_stellarmass=stackLines(galaxy, velocity,  
                             binmap, binedge,  'stellarmass',
                             cubeCO = cubeCO,
                             cubeHCN = cubeHCN, cubeHCOp = cubeHCOp,
                             cube13CO = cube13CO, cubeC18O = cubeC18O,
                             sfrmap = sfrmap, ltirmap = ltirmap,
                             stellarmap = stellarmap)

    stack_stellarmass.add_column(Column(np.tile('stellarmass',len(stack_stellarmass))),name='bin_type',index=0)

    # remove excess bins by requiring that the number of spectra in the stack 
    # keep increasing.
    stack_stellarmass.sort('bin_mean',reverse=True)
    print(stack_stellarmass['bin_type','bin_mean','stack_weights'])
    
    delta_pix = stack_stellarmass['stack_weights'][1:] - stack_stellarmass['stack_weights'][0:-1]
    
    # if difference is very small ignore.
    delta_pix[np.where( (delta_pix <0 ) & (delta_pix >-5))] = 1

    lastidx = np.argmax(delta_pix < 0)+1

    if lastidx == 1:
        lastidx = len(stack_stellarmass)

    orig_len = len(stack_stellarmass)
    stack_stellarmass = stack_stellarmass[0:lastidx]
    new_len = len(stack_stellarmass)
    
    print("Removed " + str(orig_len - new_len) + " of " + str(orig_len) + " spectra from stellarmass stack")

    ## create intensity bins
    #binmap, binedge, binlabels = makeBins(galaxy, comap, 'intensity', outDir)  
    #plotBins(galaxy, binmap, binedge, binlabels, 'intensity', outDir) 

    # do intensity stack
    #stack_intensity=stackLines(galaxy, velocity, 
    #                       binmap, binedge, 'intensity', 
    #                       cubeCO = cubeCO,
    #                      cubeHCN = cubeHCN, cubeHCOp = cubeHCOp,
    #                      cube13CO = cube13CO, cubeC18O = cubeC18O,
    #                      sfrmap = sfrmap, ltirmap = ltirmap,
    #                      stellarmap = stellarmap) 

    # stack_intensity.add_column(Column(np.tile('intensity',len(stack_intensity))),name='bin_type',index=0)

    # # remove excess bins by requiring that the number of spectra in the stack
    # # keeping increasing.
    # stack_intensity.sort('bin_mean',reverse=True)
    # print(stack_intensity['bin_type','bin_mean','stack_weights'])

    # delta_pix = stack_intensity['stack_weights'][1:] - stack_intensity['stack_weights'][0:-1]
    
    # # if difference is very small skip.
    # delta_pix[np.where( (delta_pix <0 ) & (delta_pix >-5))] = 1

    # lastidx = np.argmax(delta_pix < 0)+1
    
    # if lastidx == 0:
    #     lastidx = len(stack_intensity)

    # orig_len = len(stack_intensity)
    # stack_intensity = stack_intensity[0:lastidx]
    # new_len = len(stack_intensity)

    # print("Removed " + str(orig_len - new_len) + " of " + str(orig_len) + " spectra from intensity stack")
    
    #full_stack = vstack([stack_radius,stack_r25, stack_stellarmass, stack_intensity])
    full_stack = vstack([stack_radius,stack_r25, stack_stellarmass])

    full_stack.add_column(Column(np.tile(galaxy['NAME'],len(full_stack))),name='galaxy',index=0)

    return full_stack

def stackLines(galaxy, velocity, 
               binmap, binedge, bintype, 
               cubeCO = None,
               cubeHCN = None,
               cubeHCOp = None,
               cube13CO = None,
               cubeC18O = None,
               sfrmap = None, 
               ltirmap = None,
               stellarmap = None,
               maxAbsVel = 250.0):

    '''
    Actually do the stacking.

    cube: data we are stacking (SpectralCube)

    galaxy: line from degas_base.fits table with galaxy information.
    
    velocity: velocity map to stack data using

    binmap: bin map

    binedge: bin edges
    
    binlabel: bin labels
    
    bintype: type of bin
    
    cubeCO: CO cube

    cubeHCN: HCN cube
    
    cubeHCOp: HCO+ cube
    
    cube13CO: 13CO cube
    
    cubeC18O: C18O cube

    sfrmap: star formation rate map

    ltirmap: LTIR map

    stellarmap: stellar mass map

    maxAbsVel: absolute maximum velocity to include in spectral in km/s. 
    Assumes line is centered at zero (which should be true for stacking).
    
    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/03/2020  A.A. Kepley     Added comments and moved bin creation up 
                                a level to simplify code.
    12/10/2020  A.A. Kepley     added LTIR calculation
    5/6/2021    A.A. Kepley     modified to fit all lines for one galaxy at 
                                once so I can use CO FWHM to calculate upper 
                                limits for other lines
    9/2/2021    A.A. Kepley     Added code to calculate the stellar mass 
                                surface density. Cleaned up units.

    '''

    from signal_id.stacking import bin_by_label as BinByLabel

    # get the relevant info on the galaxy and cube
    D = galaxy['DIST_MPC'] * u.Mpc
    pix_area = (np.radians(np.abs(cubeCO.header['CDELT1']))*D.to(u.pc))**2  #pc^2 

    # do the individual line stacks.
    # -----------------------------

    stack={}
    if cubeCO:
        stack['CO'], labelvals = BinByLabel(cubeCO,
                                            binmap.value, velocity,
                                            weight_map=None,
                                            return_weights=True)
        
    if cubeHCN:
        stack['HCN'], labelvals = BinByLabel(cubeHCN,
                                             binmap.value, velocity,
                                             weight_map=None,
                                             return_weights=True)
        
    if cubeHCOp:
        stack['HCOp'], labelvals = BinByLabel(cubeHCOp,
                                              binmap.value, velocity,
                                              weight_map=None,
                                              return_weights=True)
        
    if cube13CO:
        stack['13CO'], labelvals = BinByLabel(cube13CO,
                                              binmap.value, velocity,
                                              weight_map=None,
                                              return_weights=True)
        
    if cubeC18O:
        stack['C18O'], labelvals = BinByLabel(cubeC18O,
                                              binmap.value, velocity,
                                              weight_map=None,
                                              return_weights=True)
        
    # putting the table together
    # -------------------------

    # first add bins
    t = {'bin_lower': binedge[0:-1].value, 
         'bin_upper': binedge[1:].value, 
         'bin_mean': (binedge[0:-1].value + binedge[1:].value)/2.0}

    total_stack = QTable(t,masked=True)
    
    nstack = len(labelvals[labelvals != 99])

    # Then set up the structures for the bin-based profiles, etc.
    spectral_profile = {}
    stack_weights = {}

    stack_weights = np.zeros((nstack))

    for line in ['CO','HCN','HCOp','13CO','C18O']:
        spectral_profile[line] = np.zeros((nstack,len(stack['CO'][0]['spectral_axis']))) 

    bin_label = np.zeros(nstack)
    bin_area = np.zeros(nstack) * pix_area.unit
    bin_unit = np.full(nstack, "",dtype="S15")
    sfr_mean = np.zeros(nstack) * sfrmap.unit
    ltir_mean = np.zeros(nstack)* ltirmap.unit
    mstar_mean = np.zeros(nstack) * stellarmap.unit

    spectral_axis = np.zeros((nstack,len(stack['CO'][0]['spectral_axis']))) * stack['CO'][0]['spectral_axis'].unit

    for i in range(nstack):
        spectral_axis[i,:] = stack['CO'][i]['spectral_axis']

        bin_label[i] = stack['CO'][i]['label']

        bin_area[i]= float(sum(binmap[binmap==bin_label[i]].flatten()))*pix_area          
        bin_unit[i] = binedge[0].unit.to_string()

        stack_weights[i] = stack['CO'][i]['weights']

        # calculating the mean SFR as requested
        if sfrmap is not None:
            sfr_mean[i] = np.nanmean(sfrmap[binmap==bin_label[i]]) 
        # calculate the mean LTIR as requested
        if ltirmap is not None:
            ltir_mean[i] = np.nanmean(ltirmap[binmap==bin_label[i]]) 
        # calculate mean stellar mass as requested
        if stellarmap is not None:
            mstar_mean[i] = np.nanmean(stellarmap[binmap==bin_label[i]]) 
    
        # get the spectral profiles
        for line in ['CO','HCN','HCOp','13CO','C18O']:
            if line in stack.keys():
                spectral_profile[line][i,:] = stack[line][i]['spectrum'] 
            else:
                 spectral_profile[line][i,:] = np.full(len(stack['CO'][0]['spectral_axis']),np.nan)

    # add above items to the table
    total_stack.add_column(Column(spectral_axis),name='spectral_axis')
    for line in ['CO','HCN','HCOp','13CO','C18O']:
        total_stack.add_column(Column(spectral_profile[line], name='stack_profile_'+line,unit=cubeCO.unit))

    total_stack.add_column(Column(bin_unit,name='bin_unit'),index=3)
    total_stack.add_column(Column(bin_area,name='bin_area'),index=4) # pc^2

    total_stack.add_column(Column(stack_weights,name='stack_weights'),index=5)

    if sfrmap is not None:
        total_stack.add_column(Column(sfr_mean.to('Msun/(yr pc2)'),name='sfr_mean')) # Msun/yr/(kpc -> pc)^2 
        total_stack.add_column(Column(sfr_mean.to('Msun/(yr pc2)') * bin_area),name='sfr_total') # Msun/yr/pc^2 * (pc)^2 = Msun/Yr 

    if ltirmap is not None:
        total_stack.add_column(Column(ltir_mean)  , name='ltir_mean') # Lsun/pc^2
        total_stack.add_column(Column(ltir_mean * bin_area),name='ltir_total') # Lsun/pc^2  * pc^2   = Lsun
        
    if stellarmap is not None:
        total_stack.add_column(Column(mstar_mean), name='mstar_mean') # Msun/pc^2
        total_stack.add_column(Column(mstar_mean * bin_area), name='mstar_total') # Msun

    return total_stack

def addIntegratedIntensity(full_stack, outDir, alpha_co = 4.3*(u.Msun / u.pc**2)  /  (u.K * u.km / u.s)):
    '''

    calculate the integrated line flux and the associated noise

    Input:

    full_stack: astropy table with one row containing the stacks for
    each bin.

    outDir: directory for diagnostics plots
    
    alpha_co: alpha_co value to use to calculate molecular gas mass and mass surface density.

    Output:
    
    stack with added columns for the integrated intensity.

    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    5/13/2021   A.A. Kepley     Original Code
    9/2/2021    A.A. Kepley     Added CO mass calculation

    '''

    # get number of stacks
    nstack = len(full_stack)

    # initialize output arrays
    int_intensity_fit = {}
    int_intensity_fit_err = {}
    int_intensity_fit_uplim = {}

    int_intensity_sum = {}
    int_intensity_sum_err = {}
    int_intensity_sum_uplim = {}

    fwhm_fit = np.zeros(nstack)* full_stack['spectral_axis'].unit

    int_intensity_fit['CO'] = ma.zeros(nstack) * full_stack['stack_profile_CO'].unit * full_stack['spectral_axis'].unit
    int_intensity_fit_err['CO'] = ma.zeros(nstack)* full_stack['stack_profile_CO'].unit * full_stack['spectral_axis'].unit
    int_intensity_fit_uplim['CO'] = np.full(nstack,False)

    for line in ['CO','HCN','HCOp','13CO','C18O']:

        int_intensity_sum[line] = ma.zeros(nstack) * full_stack['stack_profile_CO'].unit * full_stack['spectral_axis'].unit
        int_intensity_sum_err[line] = ma.zeros(nstack) * full_stack['stack_profile_CO'].unit * full_stack['spectral_axis'].unit
        int_intensity_sum_uplim[line] = np.full(nstack,False)
        

    # calculate the integrated intensity from a fit and from a simple sum.
    for i in range(nstack):
        
        # fit CO line to get integration region
        line = 'CO'
        (stack_int, stack_int_err, stack_fit, uplim) = fitIntegratedIntensity(full_stack[i], line, outDir)

        # skip the CO if you can't fit anything to it.
        if stack_fit.nvarys == 1:
            int_intensity_fit['CO'][i] = np.nan
            int_intensity_fit_err['CO'][i] = np.nan
            int_intensity_fit_uplim[i] = True
            fwhm_fit[i] = np.nan

            for line in ['CO','HCN', 'HCOp', '13CO','C18O']:
                int_intensity_sum[line][i] = np.nan
                int_intensity_sum_err[line][i] = np.nan
                int_intensity_sum_uplim[line][i] = True                

        else:

            fwhm_velrange = full_stack[i]['spectral_axis'][stack_fit.eval() > 0.5 * np.max(stack_fit.eval())]
            fwhm = fwhm_velrange[-1] - fwhm_velrange[0]

            int_intensity_fit['CO'][i] = stack_int
            int_intensity_fit_err['CO'][i] = stack_int_err
            int_intensity_fit_uplim[i] = uplim
            fwhm_fit[i] = fwhm

            int_axis = full_stack[i]['spectral_axis'][stack_fit.eval() > 0.01 * np.max(stack_fit.eval())]
            velrange = [np.min(int_axis), np.max(int_axis)]
                                 
            # straight sum
            for line in ['CO','HCN', 'HCOp', '13CO','C18O']:
            
                if ( ( full_stack[i]['galaxy'] == 'NGC6946') & 
                     ( ( line == '13CO') | (line == 'C18O'))):
                
                    int_intensity_sum[line][i] = np.nan
                    int_intensity_sum_err[line][i] = np.nan
                    int_intensity_sum_uplim[line][i] = True

                else:

                    (stack_sum, stack_sum_err, uplim) = sumIntegratedIntensity(full_stack[i], 
                                                                           line, 
                                                                           outDir,
                                                                           fwhm=fwhm,
                                                                           velrange=velrange)

                    int_intensity_sum[line][i] = stack_sum
                    int_intensity_sum_err[line][i] = stack_sum_err
                    int_intensity_sum_uplim[line][i] = uplim


    # add CO measurements to table
    full_stack.add_column(Column(int_intensity_fit['CO'],name='int_intensity_fit_CO'))
    full_stack.add_column(Column(int_intensity_fit_err['CO'],name='int_intensity_fit_err_CO'))
    full_stack.add_column(Column(int_intensity_fit_uplim['CO'],name='int_intensity_fit_uplim_CO'))
    
    # calculate CO mass and add to table
    full_stack.add_column(Column(int_intensity_sum['CO']*alpha_co,name='comass_mean'))
    full_stack.add_column(full_stack['comass_mean'] * full_stack['bin_area'],name='comass_total')
    full_stack.meta['alpha_co'] = alpha_co.to_string()

    # add integrated line intensity measurements to the data table.
    for line in ['CO','HCN','HCOp','13CO','C18O']:
       
        full_stack.add_column(Column(int_intensity_sum[line],name='int_intensity_sum_'+line))
        full_stack.add_column(Column(int_intensity_sum_err[line],name='int_intensity_sum_err_'+line))
        full_stack.add_column(Column(int_intensity_sum_uplim[line],name='int_intensity_sum_uplim_'+line))
        
    full_stack.add_column(Column(fwhm_fit),name='FWHM_fit')

    # Calculate line ratios and add to data table

    full_stack = calcLineRatio(full_stack,'HCN','CO')
    full_stack = calcLineRatio(full_stack,'HCOp','CO')

    full_stack = calcLineRatio(full_stack,'13CO','CO')
    full_stack = calcLineRatio(full_stack,'C18O','CO')

    full_stack = calcLineRatio(full_stack,'13CO','C18O')
    full_stack = calcLineRatio(full_stack,'HCOp','HCN')

    full_stack = calcOtherRatio(full_stack,'ltir_mean','HCN')
    full_stack = calcOtherRatio(full_stack,'ltir_mean','CO')

    return full_stack

def calcLineRatio(full_stack,line1, line2):
    '''
    calculate arbitrary line ratios and add to stack

    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    9/9/2021    A.A. Kepley     Original Code
    
    '''
    
    if 'int_intensity_sum_'+line1 not in full_stack.columns:
        print('line '+line1+' not in stack')
        return

    if 'int_intensity_sum_'+line2 not in full_stack.columns:
        print('line '+line2+' not in stack')
        return

    ratio = full_stack['int_intensity_sum_'+line1] / full_stack['int_intensity_sum_'+line2]

    error = ratio * \
            np.sqrt( (full_stack['int_intensity_sum_err_'+line1]/full_stack['int_intensity_sum_'+line1])**2 + \
                  (full_stack['int_intensity_sum_err_'+line2]/full_stack['int_intensity_sum_'+line2])**2)

    valid = full_stack['int_intensity_sum_uplim_'+line1] & full_stack['int_intensity_sum_uplim_'+line2]

    uplim = full_stack['int_intensity_sum_uplim_'+line1] & \
            np.invert(full_stack['int_intensity_sum_uplim_'+line2])


    lolim = np.invert(full_stack['int_intensity_sum_uplim_'+line1]) & \
             full_stack['int_intensity_sum_uplim_'+line2]
           
    full_stack.add_column(MaskedColumn(ratio.value,name='ratio_'+line1+'_'+line2,mask=valid))
    full_stack.add_column(MaskedColumn(error.value,name='ratio_'+line1+'_'+line2+'_err',mask=valid))
    
    full_stack.add_column(MaskedColumn(lolim,name='ratio_'+line1+'_'+line2+'_lolim',mask=valid))

    full_stack.add_column(MaskedColumn(uplim,name='ratio_'+line1+'_'+line2+'_uplim',mask=valid))

    return full_stack

    
def calcOtherRatio(full_stack,quant,line):
    '''
    calculate ratio of arbitrary input to line
    
    Date        Programmer      Description of Changes
    --------------------------------------------------
    9/9/2021    A.A. Kepley     Original Code

    '''

    if quant not in full_stack.columns:
        print(quant+' not in stack')
        return

    if 'int_intensity_sum_'+line not in full_stack.columns:
        print('line ' + line + ' not in stack')
        return

    ratio = full_stack[quant] / full_stack['int_intensity_sum_'+line]

    error = ratio * (full_stack['int_intensity_sum_err_'+line]/full_stack['int_intensity_sum_'+line])

    lolim =  full_stack['int_intensity_sum_uplim_'+line]

    full_stack.add_column(Column(ratio,name='ratio_'+quant+'_'+line))
    full_stack.add_column(Column(error,name='ratio_'+quant+'_'+line+'_err'))
    full_stack.add_column(Column(lolim),name='ratio_'+quant+'_'+line+'_lolim')

    return full_stack


def sumIntegratedIntensity(stack, line, outDir, fwhm=None, velrange=[-250,250]*(u.km/u.s), snThreshold=3.0):
    '''
    calculate the straight sum of the integrated intensity.
    
    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    5/13/2021   A.A. Kepley     Original Code

    '''
    from scipy.ndimage import label

    # default is to scale to normal distribution
    from scipy.stats import median_absolute_deviation as mad 

    spectral_axis = stack['spectral_axis']
    stack_profile = stack['stack_profile_'+line]

    chanwidth = spectral_axis[1] - spectral_axis[0]

    lineFreeChans =  ((spectral_axis > velrange[1])  | (spectral_axis < velrange[0])) & (stack_profile != 0)

    # mad is already scaled to gaussian distribution
    noisePerChan = mad(stack_profile[lineFreeChans]) * stack_profile.unit

    lineChans = (spectral_axis < velrange[1] ) & (spectral_axis > velrange[0])
    
    stack_sum = np.sum(stack_profile[lineChans]*chanwidth)
    stack_sum_err = np.sqrt(fwhm/chanwidth) * chanwidth * noisePerChan

    if stack_sum > (snThreshold * stack_sum_err):
        uplim = False
    else:
        stack_sum = stack_sum_err * snThreshold
        uplim = True

    # make plot
    plt.clf()
    fig, myax = plt.subplots(nrows=1, ncols=1, figsize=(8,6))
    plt.axhline(0,color='gray',linestyle=':')
    plt.plot(spectral_axis, stack_profile, label='data',color='orange')
    plt.xlabel('Velocity - ' + spectral_axis.unit.to_string())
    plt.ylabel('Average Intensity - ' + stack_profile.unit.to_string())

    plt.title(stack['galaxy'] + ' ' + line + ' ' + stack['bin_type'] + ' ' + str(stack['bin_mean']))
    plt.text(0.07, 0.95, "Noise="+noisePerChan.to_string(),transform=myax.transAxes)

    plt.axhspan(-3.0*noisePerChan.value, 3.0*noisePerChan.value, color='gray', alpha=0.2)
    
    plt.axvspan(spectral_axis[lineChans][0].value,spectral_axis[lineChans][-1].value, color='blue',alpha=0.2)

    lineFreeRegs, nregs = label(lineFreeChans)

    for i in range(0, nregs+1):
        if np.all(lineFreeChans[lineFreeRegs == i]):
            plt.axvspan(spectral_axis[lineFreeRegs == i][0].value,
                        spectral_axis[lineFreeRegs == i][-1].value,
                        color='green',alpha=0.2)            

    plt.legend(loc='upper right')
    plotname = stack['galaxy'] + '_' + line + '_' +  stack['bin_type'] + '_'+str(stack['bin_mean'])+'_sum.png'
    plt.savefig(os.path.join(outDir,plotname))
    plt.close()

    return stack_sum, stack_sum_err, uplim
        

def fitIntegratedIntensity(stack, line, outDir, fwhm=None, maxAbsVel=250 * u.km/u.s):
    '''
    
    calculate integrated intensity via a gaussian fit

    input: 

        stack: single stack
    
        outDir: output directory for plots and diagnostics
    
        fwhm: fwhm to use for upper limit estimate

        maxAbsVel: maximum velocity at which we expect emission.
    
        snThreshold: S/N threshold for peak finding


    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    5/13/2021   A.A. Kepley     Original Code

    '''
    
    from matplotlib import gridspec

    # default is to scale to normal distribution
    from scipy.stats import median_absolute_deviation as mad 

    #from astropy.modeling import models, fitting
    from scipy import integrate
    #from scipy.stats import f

    from lmfit.models import GaussianModel, ConstantModel

    spectral_axis = stack['spectral_axis']
    stack_profile = stack['stack_profile_'+line]

    chanwidth = spectral_axis[1] - spectral_axis[0]

    lineFreeChans = ((spectral_axis > maxAbsVel ) | (spectral_axis < - maxAbsVel)) & \
                    (stack_profile.value != 0)
    lineChans = (spectral_axis < maxAbsVel ) & (spectral_axis > - maxAbsVel)

    noisePerChan = mad(stack_profile[lineFreeChans]) * stack_profile.unit # mad is already scaled to gaussian distribution
    weights = np.ones(len(stack_profile)) / noisePerChan.value

    # setup plot
    plt.clf()
    fig = plt.figure(figsize=(8,6))
    gs = gridspec.GridSpec(2,1,height_ratios=[4,1])
    ax0 = plt.subplot(gs[0])
    ax1 = plt.subplot(gs[1])
    #fig, myax = plt.subplots(nrows=2, ncols=1,figsize=(8,6))    

    ax0.axhline(0,color='gray',linestyle=':')
    ax0.plot(spectral_axis, stack_profile,label='data')

    ax0.set_ylabel('Average Intensity - ' + stack_profile.unit.to_string())
                           
    ax0.set_title(stack['galaxy'] + ' ' + line + ' ' +  stack['bin_type'] + ' ' + str(stack['bin_mean']))
    ax0.text(0.07,0.95,"Noise="+noisePerChan.to_string(),transform=ax0.transAxes)
    ax0.axhspan(-3.0*noisePerChan.value, 3.0*noisePerChan.value, color='gray', alpha=0.2)

    ax1.axhline(0,color='gray',linestyle=':')
    ax1.set_xlabel('Velocity - ' + spectral_axis.unit.to_string())
    ax1.set_ylabel('Fit Residuals - ' + stack_profile.unit.to_string())

    # Start with simple DC model
    dcOffset = ConstantModel()
    pars_dc = dcOffset.make_params(c=0)
    dcOffsetFit = dcOffset.fit(stack_profile.value,pars_dc, x=spectral_axis.value, weights=weights)
    #print(dcOffsetFit.fit_report())

    ax0.axhline(dcOffsetFit.best_fit,label='DC Offset',color='gray')

    ax0.text(0.07,0.9,'DC BIC='+str(dcOffsetFit.bic), transform=ax0.transAxes)

    # Fit Single Gaussian

    g1 = GaussianModel()

    pars_g1 = g1.guess(stack_profile.value,x=spectral_axis.value)
    pars_g1['sigma'].max = 200.0
    #pars_g1['amplitude'].min = 0.0
    pars_g1['amplitude'].min = noisePerChan.value

    g1Fit = g1.fit(stack_profile.value,pars_g1,x=spectral_axis.value,weights=weights)

    #print(g1Fit.fit_report())

    ax0.plot(spectral_axis,g1Fit.best_fit,label='1 Gauss')
    
    ax0.text(0.07,0.85,'1 Gauss BIC='+str(g1Fit.bic), transform=ax0.transAxes)

    if (dcOffsetFit.bic > g1Fit.bic):
        # single gaussian is better than line, so try a 2 gaussian fit.

        # Fit 2 gaussians to compare to single gauss fit
        g2 = GaussianModel(prefix='g1_') + GaussianModel(prefix='g2_')
        pars_g2 = g2.make_params(g1_amplitude = g1Fit.params['amplitude'].value/2.0,
                                 g1_sigma = g1Fit.params['sigma'].value/2.0,
                                 g1_center = g1Fit.params['center']-g1Fit.params['sigma'],
                                 g2_amplitude = g1Fit.params['amplitude'].value/2.0,
                                 g2_sigma = g1Fit.params['sigma'].value/2.0,
                                 g2_center = g1Fit.params['center']+g1Fit.params['sigma'])

        #pars_g2['g1_center'].min = -maxAbsVel.value
        #pars_g2['g1_center'].max = maxAbsVel.value
        pars_g2['g1_sigma'].max = 200.0
        #pars_g2['g1_amplitude'].min = 0.0
        pars_g2['g1_amplitude'].min = noisePerChan.value

        #pars_g2['g2_center'].min = -maxAbsVel.value
        #pars_g2['g2_center'].max = maxAbsVel.value
        pars_g2['g2_sigma'].max = 200.0
        #pars_g2['g2_amplitude'].min = 0.0
        pars_g2['g2_amplitude'].min = noisePerChan.value

        g2Fit = g2.fit(stack_profile.value, pars_g2, x=spectral_axis.value,weights=weights)

        # print(g2Fit.fit_report())

        ax0.plot(spectral_axis,g2Fit.best_fit,label='2 Gauss')
        
        ax0.text(0.07,0.8,'2 Gauss BIC='+str(g2Fit.bic), transform=ax0.transAxes)

        if g2Fit.bic > g1Fit.bic:
            # single gaussian fit best -- revert to single gaussian
            stack_int = integrate.trapezoid(g1Fit.best_fit*stack_profile.unit,
                                            x=spectral_axis)
            
            # get from fit
            stack_fit = g1Fit
            fwhm = g1Fit.values['fwhm'] * spectral_axis.unit
            stack_int_err = np.sqrt(fwhm/chanwidth) * chanwidth * noisePerChan                                    
            ax0.text(0.07,0.75,'Best: 1 Gauss', transform=ax0.transAxes)

            ax1.plot(spectral_axis,g1Fit.residual)

        else:
         
            # two gaussian fit best
            stack_int = integrate.trapezoid(g2Fit.best_fit*stack_profile.unit,
                                            x=spectral_axis)

            # calculate from fit
            stack_fit = g2Fit
            fwhm_velrange = spectral_axis[g1Fit.eval() > 0.5 * np.max(g1Fit.eval())]
            fwhm = (fwhm_velrange[-1] - fwhm_velrange[0])
            stack_int_err = np.sqrt(fwhm/chanwidth) * chanwidth * noisePerChan

            ax0.text(0.07,0.75,'Best: 2 Gauss', transform=ax0.transAxes)
            
            ax1.plot(spectral_axis,g2Fit.residual)
            
        uplim = False

    elif fwhm:

        # dc offset is best fit. Estimate upper limit based on FWHM and S/N threshold
        stack_int_err = np.sqrt(fwhm/chanwidth) * chanwidth * noisePerChan
        stack_int = snThreshold * stack_int_err
        uplim = True
        stack_fit = dcOffsetFit

        ax0.text(0.07,0.8,'Best: DC', transform=myax.transAxes)

        ax1.plot(spectral_axis,dcOffsetFit.residual)

    else:
        stack_int = np.nan * spectral_axis.unit * stack_profile.unit
        stack_int_err = np.nan* spectral_axis.unit * stack_profile.unit
        fwhm = np.nan* spectral_axis.unit 
        uplim = True
        stack_fit = dcOffsetFit

    ax0.legend(loc='upper right')
    plotname = stack['galaxy'] + '_' + line + '_' +  stack['bin_type'] + '_'+str(stack['bin_mean'])+'_fit.png'
    plt.savefig(os.path.join(outDir,plotname))
    plt.close()


    return stack_int, stack_int_err, stack_fit, uplim


def makeStellarmassBins(galaxy, basemap,  bintype, outDir, binspace=0.25):
    '''
    Create bins for the data

    basemap: map used to create bins (SpectralCube Projection)

    bintype: type of bins ('intensity','stellarmass', 'radius')
    
    outDir: directory to write output bin image

     Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/3/2020   A.A. Kepley     Added more comments plus moved GCR map 
                                calculate up to other code.
    9/16/2021   A.A. Kepley     Fixing up bins
    10/21/2021  A.A. Kepley     More fixing up of bins.
    '''
    
    #Bin the basemap by brightness
    # go a little lower and a little higher to make sure that you get
    # the whole range and avoid rounding issues
    minval = np.nanmin(basemap.value)*0.999
    logminval = np.log10(minval*0.999)
    logmaxval = np.log10(np.nanmax(basemap.value)*1.001)
    
    nbins = int(np.round((logmaxval - logminval)/binspace))
    binedge = np.logspace(logminval, logmaxval, num=nbins, base=10) * basemap.unit

    bins = np.digitize(basemap.value, binedge.value) #this will automatically add an extra bin in the end for nan values
    binlabels = ['{0:1.2f}'.format(i)+basemap.unit.to_string() for i in binedge] #need to add units to stellarmass map!!

    ## Set nan values to nonsense
    bins[np.isnan(basemap.value)]  = 99

    # warn if valid valures are outside map min
    if 0 in np.unique(bins):
       print('WARNING: Value below map minimum\n') 
    if len(binedge) in np.unique(bins):
        print('WARNING: Value above map maximum\n')

    # make bins map 
    binmap = Projection(bins,wcs=basemap.wcs,header=basemap.header)
    binmap.write(os.path.join(outDir,galaxy['NAME'].upper()+'_binsby'+bintype+'.fits'), overwrite=True)
    
    return binmap, binedge, binlabels


def makeRadiusBins(galaxy, basemap,  outDir, beam=15.0, r25=False):
    '''
    Create bins for the data

    basemap: map used to create bins (SpectralCube Projection)
    
    outDir: directory to write output bin image

     Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/3/2020   A.A. Kepley     Added more comments plus moved GCR map 
                                calculate up to other code.
    4/15/2021   A.A. Kepley     Changes bins to be beam width apart in radius.
    '''

    if r25:
        minrad = 0.05
        maxrad = (90.0/3600.0) / galaxy['R25_DEG'] # go out to edge of the field.

        binedge = np.arange(minrad, maxrad, 0.05)  
        binedge = np.insert(binedge,0,0) # insert the center of the galaxy
        binedge = binedge * basemap.unit

    else:

        minrad = 0.0+beam/2.0
        #maxrad = np.max(basemap).value + beam # want to add one bin beyond to capture max.
        maxrad = 90.0 # go out to edge of field. radius ~ 60arcsec
    
        binedge = np.arange(minrad, maxrad, beam)  
        binedge = np.insert(binedge,0,0) # insert the center of the galaxy
        binedge = binedge * basemap.unit
        
    bins = np.digitize(basemap.value,binedge.value) 

    # setting the outer edges to nonsense value
    bins[bins==np.max(bins)]  = 99

    if 0 in np.unique(bins):
       print('WARNING: Value below map minimum\n') 
    if len(binedge) in np.unique(bins):
        print('WARNING: Value above map maximum\n')
    
    binlabels = ['{0:1.2f} '.format(i)  for i in binedge]

    # make bins map 
    binmap=Projection(bins, wcs=basemap.wcs, header=basemap.header)
    if r25:
        binmap.write(os.path.join(outDir,galaxy['NAME'].upper()+'_binsbyr25.fits'), overwrite=True) 
    else:
        binmap.write(os.path.join(outDir,galaxy['NAME'].upper()+'_binsbyradius.fits'), overwrite=True)

    return binmap, binedge, binlabels

def plotBins(galaxy, binmap, binedge, binlabels, bintype, outDir):
    '''
    visualize binning

    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/3/2020   A.A. Kepley     Added comments
    4/29/2021   A.A. Kepley     Simplified plotting code

    '''

    ## TODO: can I fix the plots here with an explicit color map and normalize:
    ##   norm = mpl.colors.Normalize(vmin=min(degas[dr1]['LOGMSTAR']), vmax=max(degas[dr1]['LOGMSTAR']))
    ##    cscale = mpl.cm.ScalarMappable(norm=norm,cmap='viridis')
    ##          for cscale, I want to probably set_extremes(under=None,over=None) so I can set the 99 values to gray or red and the masked values to gray or red as well.
    ##  plt.imshow(c=cscale)


    maxval = np.max(binmap[binmap.value != 99].value)
    mapvals = np.arange(1.0,maxval+1 )   


    plt.subplot(projection=binmap.wcs)
    heatmap = plt.imshow(binmap.value,origin='lower',vmin=1,vmax=maxval)

    plt.colorbar(heatmap, boundaries=binedge, values=mapvals)

    plt.xlabel('RA (J2000)')
    plt.ylabel('Dec (J2000)')

    plt.savefig(os.path.join(outDir,galaxy['NAME'].upper()+'_binsby'+bintype+'.png'))
    plt.clf()
    plt.close()


def mapCO(galaxy, regridDir, outDir, mask=None ):

    '''
    
    get CO map for galaxy
    
    galaxy: line from degas_base.fits with galaxy properties

    regridDir: input directory with regridded data

    outDir: output directory

    
     Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/3/2020   A.A. Kepley     Added comments and clarified inputs
    10/7/2021   A.A. Kepley     Removing sncut parameter and instead reading 
                                in mom0 and error produced earlier.

    '''

    # read in CO cube
    cofile = os.path.join(regridDir, galaxy['NAME']+'_12CO10_r21_simple_regrid.fits')
    if not os.path.exists(cofile):
        cofile = os.path.join(regridDir, galaxy['NAME']+'_12CO10_regrid.fits')
    
    cube = SpectralCube.read(cofile).with_spectral_unit(u.km / u.s)       
              
    # read in CO moment map
    mom0file = os.path.join(regridDir,galaxy['NAME']+'_12CO10_r21_simple_mom0_regrid.fits')
    if not os.path.exists(mom0file):
        mom0file =  os.path.join(regridDir,galaxy['NAME']+'_12CO10_mom0_regrid.fits')

    hdu = fits.open(mom0file)[0]
    data = hdu.data

    ## is this what I want below?
    if mask:
        data[np.isnan(mask)]=np.nan #apply SN mask (SN >3)

    comap = Projection(data,header=hdu.header,wcs=WCS(hdu.header),unit=hdu.header['BUNIT']) 
    comap.quicklook()

    plt.savefig(os.path.join(outDir,galaxy['NAME']+'_CO.png'))
    plt.clf()
    plt.close()

    return cube, comap

    # sncut=3.0

    # # read in cube
    # cube = SpectralCube.read(os.path.join(regridDir, galaxy['NAME']+'_12CO_regrid.fits'))
    # cube = cube.with_spectral_unit(u.km / u.s)

    # # read in mask
    # mask = SpectralCube.read(os.path.join(regridDir, galaxy['NAME']+'_12CO_mask_regrid.fits'))
    # mask = mask.with_spectral_unit(u.km / u.s)

    # # calculate noise
    # madstd = cube.mad_std(how='cube') #K #raw cube
    # chanwidth = np.abs(cube.spectral_axis[0]-cube.spectral_axis[1]) #channel width is same for all channels, km/s
    # masksum = mask.sum(axis=0) #map number of unmasked pixels 
    # noise = np.sqrt(masksum)*(madstd*chanwidth) #moment0 error map, in K km/s ## TODO: CHECK MATH HERE

    # #mask datacube
    # masked_cube = cube.with_mask(mask==1.0*u.dimensionless_unscaled) 
    # mom0 = masked_cube.moment(order=0)
    # snmap = mom0/noise #should be unitless #mom0 is from masked cube
    # snmap[snmap==np.inf]=np.nan #convert division by zero to nan

    # # write the resulting map to fits 
    # snmap.write(os.path.join(outDir, galaxy['NAME'].upper()+'_SNmap.fits'), overwrite=True)
    # snmap.quicklook()
    # plt.savefig(os.path.join(outDir, galaxy['NAME'].upper()+'_SNmap.png'))
    # plt.close()
    # plt.clf()

    # #get rid of parts of mom0 where S/N < S/N cut
    # mom0cut = mom0.copy()
    # sn = np.nan_to_num(snmap.value)
    # mom0cut[sn<sncut] = np.nan #blank out low sn regions

    # #use sigma-clipped mom0 as new 2D mask for the original cube to preserve noise in signal-free channel
    # mom0mask = ~np.isnan(mom0cut)
    # masked_cube = cube.with_mask(mom0mask) #use for stacking later

    # return mom0cut, masked_cube, mom0mask

def mapStellar(galaxy, regridDir, outDir, mask=None):
    '''
    make stellarmass map

    
    galaxy: line from degas_base.fits table with galaxy information

    mom0cut: S/N cut on mom0

    regridDir: input data directory with all regridded data

    outDir: output data directory

     Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/3/2020   A.A. Kepley     Added comments and clarified inputs
    10/07/2021  A.A. Kepley     made mask optional

    '''

    # open the stellar mass
    stellarhdu = fits.open(os.path.join(regridDir,galaxy['NAME']+'_mstar_gauss15_regrid.fits'))[0]

    stellar = stellarhdu.data
    #stellar[starmask==1.0]=np.nan #apply star mask ## AAK: I think I can skip this.
    if mask:
        stellar[np.isnan(mask)] = np.nan #apply SN mask (SN >3)

    w = WCS(stellarhdu.header)

    hdrunit = stellarhdu.header['BUNIT'].replace('MSUN','Msun').replace('PC','pc')

    stellarmap = Projection(stellar,header=stellarhdu.header,wcs=w, unit=hdrunit) 
    stellarmap.quicklook()

    plt.savefig(os.path.join(outDir,galaxy['NAME']+'_stellarmass.png'))
    plt.clf()
    plt.close()

    return stellarmap

def mapLTIR(galaxy, regridDir, outDir, mask=None):
    '''
    make LTIR map

    
    galaxy: line from degas_base.fits table with galaxy information

    mom0cut: S/N cut on mom0

    regridDir: input data directory with all regridded data

    outDir: output data directory

     Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    12/10/2020  A.A. Kepley      Original code based on mapStellar

    '''

    hdu=fits.open(os.path.join(regridDir,galaxy['NAME']+'_LTIR_gauss15_regrid.fits'))[0]

    data=hdu.data

    if mask:
        data[np.isnan(mask)]=np.nan #apply SN mask (SN >3)

    LTIRmap=Projection(data,header=hdu.header,wcs=WCS(hdu.header),unit=hdu.header['BUNIT']) 
    LTIRmap.quicklook()

    plt.savefig(os.path.join(outDir,galaxy['NAME']+'_LTIR.png'))
    plt.clf()
    plt.close()

    return LTIRmap

def mapSFR(galaxy, regridDir, outDir, mask=None):
    '''
    import sfr map from W4+FUV

     Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/3/2020  A.A. Kepley     Added comments and clarified inputs
    10/7/2021   A.A. Kepley     Made mask optional
    '''
    
    sfrhdu = fits.open(os.path.join(regridDir,galaxy['NAME']+'_sfr_fuvw4_gauss15_regrid.fits'))[0]
    sfr = sfrhdu.data
    if mask:
        sfr[np.isnan(mask)] = np.nan

    w = WCS(sfrhdu.header)
    
    hdrunit = sfrhdu.header['BUNIT'].replace('MSUN','Msun')
    hdrunit = hdrunit.replace("KPC","kpc")
    hdrunit = hdrunit.replace('YR','yr')

    sfrmap=Projection(sfr,header=sfrhdu.header,wcs=w, unit=hdrunit) 
    sfrmap.quicklook()
    
    # save plot of map
    plt.savefig(os.path.join(outDir,galaxy['NAME']+'_sfr.png'))
    plt.clf()
    plt.close()
    
    return sfrmap

def mapGCR(galaxy,  basemap):
    '''
    Create map of galactic radii

    Date        Programmer      Description of Changes
    ----------------------------------------------------------------------
    10/29/2020  Yiqing Song     Original Code
    12/03/2020  A.A. Kepley     Tweak to how input data is handled and 
                                added comments
    '''

    # get the data we need for the galaxy
    ra = galaxy['RA_DEG']
    dec = galaxy['DEC_DEG']
    inc = np.radians(galaxy['INCL_DEG'])
    pa = np.radians(galaxy['POSANG_DEG'])
    r25 = galaxy['R25_DEG'] * u.deg
    Dmpc = galaxy['DIST_MPC']
    
    # get wcs
    w = basemap.wcs

    # get center
    x0,y0 = w.all_world2pix(ra,dec,0,ra_dec_order=True)

    # get coordinates
    y = np.arange(0,np.shape(basemap)[0],1)
    x = np.arange(0,np.shape(basemap)[1],1)

    # create a 2d image of coordinates
    xx,yy = np.meshgrid(x,y)

    # calculate the radius in pixels
    xx_new = x0+(xx-x0)*np.cos(pa)+(yy-y0)*np.sin(pa) 
    yy_new = y0-(xx-x0)*np.sin(pa)+(yy-y0)*np.cos(pa) 
    R = np.sqrt((xx_new-x0)**2/np.cos(inc)**2+(yy_new-y0)**2) #pixels

    # now convert from pixels to actual units
    head = basemap.header
    pxscale = np.radians(np.abs(head['CDELT1'])) #radian/pixel

    R_arcsec = np.degrees(R*pxscale)*3600.0 * u.arcsec# map of GCR in arcsec
    R_kpc = R*pxscale*Dmpc*1000 * u.kpc# map of GCR in kpc
    R_r25 = R_arcsec/r25.to(u.arcsec) # map of GCR in units of R25

    Rarcsec_map = Projection(R_arcsec, header=head, wcs=w, unit=R_arcsec.unit)
    Rkpc_map = Projection(R_kpc, header=head, wcs=w, unit=R_kpc.unit) 
    Rr25_map = Projection(R_r25, header=head, wcs=w) 

    # map of galactocentric radius in unit of kpc, arcmin, in r25
    return Rarcsec_map, Rkpc_map, Rr25_map 






    


    
    

