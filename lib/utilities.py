# Created on Tue Dec 23 21:15:19 2014

# Author: XiaoTao Wang
# Organization: HuaZhong Agricultural University

import logging, zipfile, tempfile, os
import numpy as np
from mirnylib.genome import Genome
from numpy.lib.format import write_array
from hiclib.fragmentHiC import HiCdataset
from mirnylib.numutils import uniqueIndex, fillDiagonal
from mirnylib.h5dict import h5dict

log = logging.getLogger(__name__)

# A customized HiCdataset class, which makes filtering processes more flexible
class cHiCdataset(HiCdataset):
    
    def parseInputData(self, dictLike, commandArgs, **kwargs):
        """
        Added Parameters
        ----------------
        commandArgs : NameSpace
            A NameSpace object defined by argparse.            
        """
        ## Necessary Modules
        import numexpr
        
        if not os.path.exists(dictLike):
            raise IOError('File not found: %s' % dictLike)
        
        dictLike = h5dict(dictLike, 'r')
        self.chrms1 = dictLike['chrms1']
        self.chrms2 = dictLike['chrms2']
        self.cuts1 = dictLike['cuts1']
        self.cuts2 = dictLike['cuts2']
        self.strands1 = dictLike['strands1']
        self.strands2 = dictLike['strands2']
        self.dists1 = np.abs(dictLike['rsites1'] - self.cuts1)
        self.dists2 = np.abs(dictLike['rsites2'] - self.cuts2)
        self.mids1 = (dictLike['uprsites1'] + dictLike['downrsites1']) / 2
        self.mids2 = (dictLike['uprsites2'] + dictLike['downrsites2']) / 2
        self.fraglens1 = np.abs(
            (dictLike['uprsites1'] - dictLike['downrsites1']))
        self.fraglens2 = np.abs(
            (dictLike['uprsites2'] - dictLike['downrsites2']))
        self.fragids1 = self.mids1 + np.array(self.chrms1,
                                              dtype='int64') * self.fragIDmult
        self.fragids2 = self.mids2 + np.array(self.chrms2,
                                              dtype='int64') * self.fragIDmult
        distances = np.abs(self.mids1 - self.mids2)
        distances[self.chrms1 != self.chrms2] = -1
        self.distances = distances  # Distances between restriction fragments
        del distances
        
        # Total Reads
        self.N = len(self.chrms1)
        
        self.metadata["100_TotalReads"] = self.N
        
        try:
            dictLike['misc']['genome']['idx2label']
            self.updateGenome(self.genome,
                              oldGenome=dictLike["misc"]["genome"]["idx2label"],
                              putMetadata=True)
        except KeyError:
            assumedGenome = Genome(self.genome.genomePath)
            self.updateGenome(self.genome, oldGenome=assumedGenome, putMetadata=True)
            
        DSmask = (self.chrms1 >= 0) * (self.chrms2 >= 0)
        self.metadata["200_totalDSReads"] = DSmask.sum()
        
        self.metadata["201_DS+SS"] = len(DSmask)
        self.metadata["202_SSReadsRemoved"] = len(DSmask) - DSmask.sum()
        
        mask = DSmask
        
        ## Information based on restriction fragments
        sameFragMask = self.evaluate("a = (fragids1 == fragids2)",
                                     ["fragids1", "fragids2"]) * DSmask
        cutDifs = self.cuts2[sameFragMask] > self.cuts1[sameFragMask]
        s1 = self.strands1[sameFragMask]
        s2 = self.strands2[sameFragMask]
        SSDE = (s1 != s2)
        SS = SSDE * (cutDifs == s2)
        Dangling = SSDE & (~SS)
        SS_N = SS.sum()
        SSDE_N = SSDE.sum()
        sameFrag_N = sameFragMask.sum()
        
        dist = self.evaluate("a = - cuts1 * (2 * strands1 -1) - "
                             "cuts2 * (2 * strands2 - 1)",
                             ["cuts1", "cuts2", "strands1", "strands2"])
        Dangling_L = dist[sameFragMask][Dangling]
        library_L = int(np.ceil((np.percentile(Dangling_L, 95))))
        self.maximumMoleculeLength = library_L
        
        readsMolecules = self.evaluate(
            "a = numexpr.evaluate('(chrms1 == chrms2) & (strands1 != strands2) &  (dist >=0) &"
            " (dist <= maximumMoleculeLength)')",
            internalVariables=["chrms1", "chrms2", "strands1", "strands2"],
            externalVariables={"dist": dist},
            constants={"maximumMoleculeLength": self.maximumMoleculeLength, "numexpr": numexpr})
        
        if commandArgs.sameFragments:
            mask *= (-sameFragMask)
            noSameFrag = mask.sum()
            self.metadata["210_sameFragmentReadsRemoved"] = sameFrag_N
            self.metadata["212_Self-Circles"] = SS_N
            self.metadata["214_DandlingEnds"] = SSDE_N - SS_N
            self.metadata["216_error"] = sameFrag_N - SSDE_N
            mask *= (readsMolecules == False)
            extraDE = mask.sum()
            self.metadata["220_extraDandlingEndsRemoved"] = -extraDE + noSameFrag
            
        if commandArgs.RandomBreaks:
            
            ini_N = extraDE
            mask *= ((self.dists1 + self.dists2) <= library_L)
            rb_N = ini_N - mask.sum()
            self.metadata["330_removeRandomBreaks"] = rb_N
        
        if mask.sum() == 0:
            raise Exception(
                'No reads left after filtering. Please, check the input data')
            
        del DSmask, sameFragMask
        del dist, readsMolecules
        
        self.metadata["300_ValidPairs"] = self.N
        
        self.maskFilter(mask)
    
    def filterDuplicates(self):
        
        Nds = self.N

        # an array to determine unique rows. Eats 16 bytes per DS record
        dups = np.zeros((Nds, 2), dtype="int64", order="C")

        dups[:, 0] = self.chrms1
        dups[:, 0] *= self.fragIDmult
        dups[:, 0] += self.cuts1
        dups[:, 1] = self.chrms2
        dups[:, 1] *= self.fragIDmult
        dups[:, 1] += self.cuts2
        dups.sort(axis=1)
        dups.shape = (Nds * 2)
        strings = dups.view("|S16")
        # Converting two indices to a single string to run unique
        uids = uniqueIndex(strings)
        del strings, dups
        stay = np.zeros(Nds, bool)
        stay[uids] = True  # indexes of unique DS elements
        del uids
        uflen = len(self.ufragments)
        Remained_N = stay.sum()
        self.metadata["320_duplicatesRemoved"] = len(stay) - Remained_N
        self.maskFilter(stay)
        assert len(self.ufragments) == uflen  # self-check
    
    def filterRsiteStart(self, offset = 5):
        """
        Removes reads that start within x bp near rsite

        Parameters
        ----------

        offset : int
            Number of bp to exclude next to rsite, not including offset

        """

        expression = "mask = (np.abs(dists1 - fraglens1) >= offset) * "\
        "((np.abs(dists2 - fraglens2) >= offset) )"
        mask = self.evaluate(expression,
                             internalVariables=["dists1", "fraglens1",
                                                "dists2", "fraglens2"],
                             constants={"offset": offset, "np": np},
                             outVariable=("mask", np.zeros(self.N, bool)))
        Remained_N = mask.sum()
        self.metadata["310_startNearRsiteRemoved"] = len(mask) - Remained_N
        self.maskFilter(mask)
        
    def filterLarge(self, cutlarge = 100000, cutsmall = 100):
        """
        Removes very large and small fragments.

        Parameters
        ----------
        cutlarge : int
            Remove fragments larger than it
        cutsmall : int
            Remove fragments smaller than it
        """
        self._buildFragments()
        
        p = (self.ufragmentlen < (cutlarge)) * (self.ufragmentlen > cutsmall)
        N1 = self.N
        self.fragmentFilter(self.ufragments[p])
        N2 = self.N
        self.metadata["340_removedLargeSmallFragments"] = N1 - N2
        self._dumpMetadata()
    
    def filterExtreme(self, cutH = 0.005, cutL = 0):
        """
        Removes fragments with most and/or least # counts

        Parameters
        ----------
        cutH : float, 0<=cutH < 1, optional
            Fraction of the most-counts fragments to be removed
            
        cutL : float, 0<=cutL<1, optional
            Fraction of the least-counts fragments to be removed
        """
        self._buildFragments()
        
        s = self.fragmentSum()
        ss = np.sort(s)

        valueL, valueH = np.percentile(ss, [100. * cutL, 100 * (1. - cutH)])
        news = (s >= valueL) * (s <= valueH)
        N1 = self.N
        self.fragmentFilter(self.ufragments[news])
        self.metadata["350_removedFromExtremeFragments"] = N1 - self.N
        self._dumpMetadata()
    
    def maskFilter(self, mask):
        """
        Use numpy's internal mask mechanism instead.

        Parameters
        ----------
        mask : array of bools
            Indexes of reads to keep
            
        """
        # Uses 16 bytes per read
        length = 0
        ms = mask.sum()
        
        assert mask.dtype == np.bool
        
        self.N = ms
        self.DSnum = self.N
        
        if hasattr(self, "ufragments"):
            del self.ufragmentlen, self.ufragments
            
        for name in self.vectors:
            data = self._getData(name)
            ld = len(data)
            if length == 0:
                length = ld
            else:
                if ld != length:
                    self.delete()
            
            newdata = data[mask]
                
            del data
            
            self._setData(name, newdata)
            
            del newdata
            
        del mask
        
        self.rebuildFragments()
    
    def saveByChromosomeHeatmap(self, filename, resolution = 40000,
                                includeTrans = False,
                                countDiagonalReads = "Once"):
        """
        Saves chromosome by chromosome heatmaps to h5dict.
        
        This method is not as memory demanding as saving all x all heatmap.

        Keys of the h5dict are of the format ["1 1"], where chromosomes are
        zero-based, and there is one space between numbers.

        Parameters
        ----------
        filename : str
            Filename of the h5dict with the output
            
        resolution : int
            Resolution to save heatmaps
            
        includeTrans : bool, optional
            Build inter-chromosomal heatmaps (default: False)
            
        countDiagonalReads : "once" or "twice"
            How many times to count reads in the diagonal bin

        """
        if countDiagonalReads.lower() not in ["once", "twice"]:
            raise ValueError("Bad value for countDiagonalReads")
            
        self.genome.setResolution(resolution)
        
        pos1 = self.evaluate("a = np.array(mids1 / {res}, dtype = 'int32')"
                             .format(res=resolution), "mids1")
        pos2 = self.evaluate("a = np.array(mids2 / {res}, dtype = 'int32')"
                             .format(res=resolution), "mids2")
                             
        chr1 = self.chrms1
        chr2 = self.chrms2
        
        # DS = self.DS  # 13 bytes per read up to now, 16 total
        mydict = h5dict(filename)

        for chrom in xrange(self.genome.chrmCount):
            if includeTrans == True:
                mask = ((chr1 == chrom) + (chr2 == chrom))
            else:
                mask = ((chr1 == chrom) * (chr2 == chrom))
            # Located chromosomes and positions of chromosomes
            c1, c2, p1, p2 = chr1[mask], chr2[mask], pos1[mask], pos2[mask]
            if includeTrans == True:
                # moving different chromosomes to c2
                # c1 == chrom now
                mask = (c2 == chrom) * (c1 != chrom)
                c1[mask], c2[mask], p1[mask], p2[mask] = c2[mask].copy(), c1[
                    mask].copy(), p2[mask].copy(), p1[mask].copy()
                del c1  # ignore c1
                args = np.argsort(c2)
                c2 = c2[args]
                p1 = p1[args]
                p2 = p2[args]

            for chrom2 in xrange(chrom, self.genome.chrmCount):
                if (includeTrans == False) and (chrom2 != chrom):
                    continue
                start = np.searchsorted(c2, chrom2, "left")
                end = np.searchsorted(c2, chrom2, "right")
                cur1 = p1[start:end]
                cur2 = p2[start:end]
                label = np.asarray(cur1, "int64")
                label *= self.genome.chrmLensBin[chrom2]
                label += cur2
                maxLabel = self.genome.chrmLensBin[chrom] * \
                           self.genome.chrmLensBin[chrom2]
                counts = np.bincount(label, minlength = maxLabel)
                assert len(counts) == maxLabel
                mymap = counts.reshape((self.genome.chrmLensBin[chrom], -1))
                if chrom == chrom2:
                    mymap = mymap + mymap.T
                    if countDiagonalReads.lower() == "once":
                        fillDiagonal(mymap, np.diag(mymap).copy() / 2)
                mydict["%d %d" % (chrom, chrom2)] = mymap
        
        mydict['resolution'] = resolution

        return
    
    def saveHiResHeatmapWithOverlaps(self, filename, resolution = 10000,
                                     countDiagonalReads = "Once",
                                     maxBinSpawn=10, chromosomes = "all"):
        """
        Creates within-chromosome heatmaps at very high resolution,
        assigning each fragment to all the bins it overlaps with,
        proportional to the area of overlaps.

        Parameters
        ----------
        resolution : int or str
            Resolution of a heatmap.
            
        countDiagonalReads : "once" or "twice"
            How many times to count reads in the diagonal bin
            
        maxBinSpawn : int, optional, not more than 10
            Discard read if it spawns more than maxBinSpawn bins

        """
        from scipy import weave

        tosave = h5dict(filename)
        
        self.genome.setResolution(resolution)
        
        if chromosomes == "all":
            chromosomes = range(self.genome.chrmCount)
            
        for chrom in chromosomes:
            mask = (self.chrms1 == chrom) * (self.chrms2 == chrom)

            if mask.sum() == 0:
                continue

            low1 = (self.mids1[mask] - self.fraglens1[mask] / 2) / float(resolution)

            high1 = (self.mids1[mask] + self.fraglens1[mask] / 2) / float(resolution)

            low2 = (self.mids2[mask] - self.fraglens2[mask] / 2) / float(resolution)

            high2 = (self.mids2[mask] + self.fraglens2[mask] / 2) / float(resolution)

            del mask

            N = len(low1)

            heatmapSize = int(self.genome.chrmLensBin[chrom])

            heatmap = np.zeros((heatmapSize, heatmapSize),
                               dtype="float64", order="C")


            code = """
            double vector1[100];
            double vector2[100];

            for (int readNum = 0;  readNum < N; readNum++)
            {
                for (int i=0; i<10; i++)
                {
                    vector1[i] = 0;
                    vector2[i] = 0;
                }

                double l1 = low1[readNum];
                double l2 = low2[readNum];
                double h1 = high1[readNum];
                double h2 = high2[readNum];


                if ((h1 - l1) > maxBinSpawn) continue;
                if ((h2 - l2) > maxBinSpawn) continue;

                int binNum1 = ceil(h1) - floor(l1);
                int binNum2 = ceil(h2) - floor(l2);
                double binLen1 = h1 - l1;
                double binLen2 = h2 - l2;

                int b1 = floor(l1);
                int b2 = floor(l2);

                if (binNum1 == 1)
                    vector1[0] = 1.;
                else
                    {
                    vector1[0] = (ceil(l1 + 0.00001) - l1) / binLen1;
                    for (int t = 1; t< binNum1 - 1; t++)
                        {vector1[t] = 1. / binLen1;}
                    vector1[binNum1 - 1] = (h1 - floor(h1)) / binLen1;
                    }

                if (binNum2 == 1) vector2[0] = 1.;

                else
                    {
                    vector2[0] = (ceil(l2 + 0.0001) - l2) / binLen2;
                    for (int t = 1; t< binNum2 - 1; t++)
                        {vector2[t] = 1. / binLen2;}
                    vector2[binNum2 - 1] = (h2 - floor(h2)) / binLen2;
                    }

                for (int i = 0; i< binNum1; i++)
                    {
                    for (int j = 0; j < binNum2; j++)
                        {
                        heatmap[(b1 + i) * heatmapSize +  b2 + j] += vector1[i] * vector2[j];
                        }
                    }
                }
                
            """
            weave.inline(code,
                         ['low1', "high1", "low2", "high2",
                           "N", "heatmap", "maxBinSpawn",
                          "heatmapSize",
                           ],
                         extra_compile_args=['-march=native  -O3 '],
                         support_code=r"""
                        #include <stdio.h>
                        #include <math.h>""")
            del high1, low1, high2, low2


            for i in xrange(len(heatmap)):
                heatmap[i, i:] += heatmap[i:, i]
                heatmap[i:, i] = heatmap[i, i:]
                
            if countDiagonalReads.lower() == "once":
                diag = np.diag(heatmap).copy()
                fillDiagonal(heatmap, diag / 2)
                del diag
            elif countDiagonalReads.lower() == "twice":
                pass
            else:
                raise ValueError("Bad value for countDiagonalReads")
            tosave["{0} {0}".format(chrom)] = heatmap
            tosave.flush()
            del heatmap
            weave.inline("")  # to release all buffers of weave.inline
            import gc
            gc.collect()
        
        tosave['resolution'] = resolution

# Convert Matrix to Sparse Matrix
def toSparse(source, idx2label, Format = 'NPZ'):
    """
    Convert intra-chromosomal contact matrices to sparse ones.
    
    Parameters
    ----------
    source : str
         Hdf5 file name.
    
    idx2label : dict
        A dictionary for conversion between zero-based indices and
        string chromosome labels.
    
    Format : {'NPZ', 'HDF5'}
        Output format. (Default: HDF5)
    
    """
    lib = h5dict(source, mode = 'r')
    
    ## Uniform numpy-structured-array format
    itype = np.dtype({'names':['bin1', 'bin2', 'IF'],
                          'formats':[np.int, np.int, np.float]})
    
    ## Create a Zip file in NPZ case
    if Format.upper() == 'NPZ':
        output = source.replace('.hm', '-sparse.npz')
        Zip = zipfile.ZipFile(output, mode = 'w', allowZip64 = True)
        fd, tmpfile = tempfile.mkstemp(suffix = '-numpy.npy')
        os.close(fd)
    
    if Format.upper() == 'HDF5':
        output = source.replace('.hm', '-sparse.hm')
        odict = h5dict(output)
    
    log.log(21, 'Sparse Matrices will be saved to %s', output)
    log.log(21, 'Only intra-chromosomal matrices will be taken into account')
    log.log(21, 'Coverting ...')
    
    for i in lib:
        if (i != 'resolution') and (len(set(i.split())) == 1):
            # Used for the dict-like key
            key = idx2label[int(i.split()[0])]
            
            log.log(21, 'Chromosome %s ...', key)
            # 2D-Matrix
            H = lib[i]
            
            # Triangle Array
            Triu = np.triu(H)
            # Sparse Matrix in Memory
            x, y = np.nonzero(Triu)
            values = Triu[x, y]
            sparse = np.zeros(values.size, dtype = itype)
            sparse['bin1'] = x
            sparse['bin2'] = y
            sparse['IF'] = values
            
            if Format.upper() == 'HDF5':
                # Really Simple, just like a dictionary
                odict[key] = sparse
            if Format.upper() == 'NPZ':
                # Much more complicated, but we need make a compatible
                # file interface for other API
                fname = key + '.npy'
                fid = open(tmpfile, 'wb')
                try:
                    write_array(fid, np.asanyarray(sparse))
                    fid.close()
                    fid = None
                    Zip.write(tmpfile, arcname = fname)
                finally:
                    if fid:
                        fid.close()
            log.log(21, 'Done!')
    
    os.remove(tmpfile)
    
    Zip.close()
        
        